"""Backend tests. No API keys, no network: gh and the providers are both stubbed."""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from tri_review.schema import Finding, ReviewOutput

DIFF = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n"

META = {
    "number": "42",
    "title": "Add a thing",
    "author": "octocat",
    "updated_at": "2026-08-01T00:00:00Z",
    "head_sha": "e" * 40,
    "state": "OPEN",
    "url": "https://github.com/octocat/Hello-World/pull/42",
}


class StubLLM:
    """A provider that always returns one finding naming itself."""

    def __init__(self, model_name, raises=None):
        self.model_name = model_name
        self.raises = raises

    def with_structured_output(self, _schema):
        return self

    def invoke(self, _messages):
        if self.raises:
            raise self.raises
        return ReviewOutput(
            findings=[
                Finding(
                    file="a.py",
                    line=1,
                    severity="major",
                    category="bug",
                    title=f"issue seen by {self.model_name}",
                    detail="detail",
                )
            ]
        )


class StubSynthLLM(StubLLM):
    """The synthesizer is called with .invoke and returns prose, not findings."""

    def invoke(self, _messages):
        if self.raises:
            raise self.raises

        class _Resp:
            content = "## Consensus Findings\n\nBoth models flagged a.py:1.\n"

        return _Resp()


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A TestClient on a throwaway database, with gh and the providers stubbed."""
    monkeypatch.setenv("TRI_REVIEW_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("TRI_REVIEW_MODEL_A", "gpt-test-a")
    monkeypatch.setenv("TRI_REVIEW_MODEL_B", "claude-test-b")
    monkeypatch.setenv("TRI_REVIEW_MODEL_C", "gemini-test-c")

    from tri_review import context as context_mod
    from tri_review import github as github_mod
    from tri_review import graph as graph_mod
    from tri_review.web import db as db_mod
    from tri_review.web.app import app

    db_mod.reset_engine()

    for module in (github_mod, graph_mod.github, context_mod.github):
        monkeypatch.setattr(module, "preflight", lambda repo=None: None)
        monkeypatch.setattr(module, "fetch_diff", lambda pr, repo=None, exclude=(): DIFF)
        monkeypatch.setattr(module, "fetch_pr_meta", lambda pr, repo=None: dict(META))
        monkeypatch.setattr(module, "fetch_file_content", lambda r, p, ref: "file body")

    app.state.llm_builder = _panel_builder()
    with TestClient(app) as test_client:
        yield test_client

    app.state.llm_builder = None
    db_mod.reset_engine()


def _panel_builder(failing: set[str] | None = None):
    failing = failing or set()

    def builder(name):
        if name in failing:
            return StubLLM(name, raises=RuntimeError(f"{name} has no key"))
        if name.startswith("gpt-test-a"):
            # Slot A is also the synthesizer; it must answer both call shapes.
            return _DualStub(name)
        return StubLLM(name)

    return builder


class _DualStub(StubLLM):
    """Slot A doubles as the synthesizer, so it answers structured and prose calls."""

    def invoke(self, messages):
        text = str(messages[0].content if messages else "")
        if "synthesizing" in text:
            return StubSynthLLM(self.model_name).invoke(messages)
        return StubLLM.invoke(self, messages)


def _await_terminal(client, review_id, timeout=10.0):
    """Poll until the background thread finishes, or fail loudly."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/api/reviews/{review_id}").json()
        if body["status"] in ("succeeded", "failed"):
            return body
        time.sleep(0.02)
    pytest.fail(f"review {review_id} never reached a terminal status")


# --- health -----------------------------------------------------------------


def test_health_reports_which_provider_keys_are_present(client, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    body = client.get("/api/health").json()

    assert body["status"] == "ok"
    assert body["provider_keys"]["openai"] is True
    assert body["provider_keys"]["anthropic"] is False
    # One key is not enough to triangulate, so the server is not ready.
    assert body["keys_present"] == 1
    assert body["ready"] is False


def test_health_is_ready_with_two_keys(client, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert client.get("/api/health").json()["ready"] is True


# --- resolving a pasted URL -------------------------------------------------


def test_resolve_turns_a_pasted_url_into_pr_identity(client):
    body = client.post(
        "/api/pulls/resolve",
        json={"url": "https://github.com/octocat/Hello-World/pull/42"},
    ).json()

    assert body["repo"] == "octocat/Hello-World"
    assert body["number"] == "42"
    assert body["title"] == "Add a thing"
    assert body["head_sha"] == "e" * 40


def test_resolve_rejects_a_non_github_url(client):
    response = client.post("/api/pulls/resolve", json={"url": "https://gitlab.com/a/b/pull/1"})
    assert response.status_code == 404
    assert "PRNotFound" in response.json()["error"]


def test_resolve_needs_a_url_or_a_repo_and_number(client):
    assert client.post("/api/pulls/resolve", json={}).status_code == 404


# --- preview ----------------------------------------------------------------


def test_preview_summarises_without_calling_a_model(client, monkeypatch):
    """The confirm step must be free -- assert no provider is ever constructed."""
    from tri_review import providers

    monkeypatch.setattr(
        providers, "build_llm", lambda name: pytest.fail("preview called a provider")
    )

    body = client.post(
        "/api/reviews/preview", json={"url": "https://github.com/octocat/Hello-World/pull/42"}
    ).json()

    assert body["repo"] == "octocat/Hello-World"
    assert body["context"]["files_included"] == ["a.py"]
    assert body["context"]["estimated_tokens"] > 0
    assert len(body["models"]) == 3


# --- running a review -------------------------------------------------------


def test_review_returns_immediately_then_completes(client):
    response = client.post(
        "/api/reviews", json={"url": "https://github.com/octocat/Hello-World/pull/42"}
    )
    assert response.status_code == 202
    review_id = response.json()["id"]

    body = _await_terminal(client, review_id)

    assert body["status"] == "succeeded"
    assert body["report_md"]
    assert body["head_sha"] == "e" * 40


def test_three_columns_come_back_in_panel_order(client):
    """Columns are keyed by the panel, not arrival order, so they don't reshuffle."""
    review_id = client.post(
        "/api/reviews", json={"url": "https://github.com/octocat/Hello-World/pull/42"}
    ).json()["id"]

    body = _await_terminal(client, review_id)

    assert [c["model"] for c in body["columns"]] == [
        "gpt-test-a",
        "claude-test-b",
        "gemini-test-c",
    ]
    assert all(c["status"] == "ok" for c in body["columns"])
    for column in body["columns"]:
        assert column["markdown"].strip(), f"{column['model']} rendered an empty column"
        assert f"issue seen by {column['model']}" in column["markdown"]


def test_a_failed_model_keeps_its_column_and_the_others_still_report(client):
    from tri_review.web.app import app

    app.state.llm_builder = _panel_builder(failing={"gemini-test-c"})

    review_id = client.post(
        "/api/reviews", json={"url": "https://github.com/octocat/Hello-World/pull/42"}
    ).json()["id"]
    body = _await_terminal(client, review_id)

    assert body["status"] == "succeeded"
    columns = {c["model"]: c for c in body["columns"]}
    assert columns["gemini-test-c"]["status"] == "failed"
    assert "has no key" in columns["gemini-test-c"]["error"]
    # The dead column still says why rather than reading as a clean review.
    assert "did not report" in columns["gemini-test-c"]["markdown"]
    assert columns["gpt-test-a"]["status"] == "ok"


def test_partial_results_survive_a_synthesis_failure(client):
    """Two models down means no consensus -- but the one review that landed is
    still the thing the user asked to see, so it must not be discarded."""
    from tri_review.web.app import app

    app.state.llm_builder = _panel_builder(failing={"claude-test-b", "gemini-test-c"})

    review_id = client.post(
        "/api/reviews", json={"url": "https://github.com/octocat/Hello-World/pull/42"}
    ).json()["id"]
    body = _await_terminal(client, review_id)

    assert body["status"] == "failed"
    assert "triangulate" in body["error"]
    columns = {c["model"]: c for c in body["columns"]}
    assert columns["gpt-test-a"]["status"] == "ok"
    assert columns["gpt-test-a"]["findings"], "the surviving review was thrown away"


def test_review_rejects_an_unknown_model(client):
    response = client.post(
        "/api/reviews",
        json={
            "url": "https://github.com/octocat/Hello-World/pull/42",
            "models": ["not-a-real-model", "also-fake"],
        },
    )
    assert response.status_code == 404
    assert "Unrecognized model" in response.json()["detail"]


def test_review_rejects_a_single_model_panel(client):
    response = client.post(
        "/api/reviews",
        json={"url": "https://github.com/octocat/Hello-World/pull/42", "models": ["gpt-test-a"]},
    )
    assert response.status_code == 404
    assert "at least 2" in response.json()["detail"]


# --- history ----------------------------------------------------------------


def test_history_lists_reviews_newest_first(client):
    ids = [
        client.post(
            "/api/reviews", json={"url": "https://github.com/octocat/Hello-World/pull/42"}
        ).json()["id"]
        for _ in range(2)
    ]
    for review_id in ids:
        _await_terminal(client, review_id)

    listed = [row["id"] for row in client.get("/api/reviews").json()]
    assert listed == sorted(ids, reverse=True)


def test_a_review_survives_a_restart(client, tmp_path):
    """The row is the source of truth, not the worker thread's memory."""
    review_id = client.post(
        "/api/reviews", json={"url": "https://github.com/octocat/Hello-World/pull/42"}
    ).json()["id"]
    _await_terminal(client, review_id)

    from tri_review.web import db as db_mod

    db_mod.reset_engine()  # as though the process had restarted

    reloaded = db_mod.get_review(review_id)
    assert reloaded is not None
    assert reloaded.status == "succeeded"
    assert reloaded.report_md


def test_unknown_review_is_a_404(client):
    assert client.get("/api/reviews/9999").status_code == 404


# --- SSE --------------------------------------------------------------------


def _sse_events(raw: str) -> list[tuple[str, dict]]:
    """Parse an SSE body into (event name, payload) pairs."""
    events = []
    name = None
    for line in raw.splitlines():
        if line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:") and name:
            events.append((name, json.loads(line[5:].strip())))
            name = None
    return events


def test_stream_replays_a_finished_review_and_closes(client):
    """A late subscriber must not hang waiting for events it already missed."""
    review_id = client.post(
        "/api/reviews", json={"url": "https://github.com/octocat/Hello-World/pull/42"}
    ).json()["id"]
    _await_terminal(client, review_id)

    with client.stream("GET", f"/api/reviews/{review_id}/events") as response:
        assert response.status_code == 200
        body = "".join(response.iter_text())

    events = _sse_events(body)
    names = [name for name, _ in events]
    assert names[0] == "snapshot"
    assert "done" in names

    snapshot = events[0][1]
    assert len(snapshot["columns"]) == 3
    assert snapshot["report_md"]


def test_stream_of_a_live_review_reaches_done(client):
    review_id = client.post(
        "/api/reviews", json={"url": "https://github.com/octocat/Hello-World/pull/42"}
    ).json()["id"]

    with client.stream("GET", f"/api/reviews/{review_id}/events") as response:
        body = "".join(response.iter_text())

    names = [name for name, _ in _sse_events(body)]
    assert names[0] == "snapshot"
    assert names[-1] == "done"


def test_results_stream_as_they_land_rather_than_batched(client):
    """The point of SSE here: a slow panel shows each model as it finishes.

    Stub models are instantaneous, which would let a purely replay-based
    implementation pass every other streaming test in this file. Slowing them
    down is what makes the difference observable: the snapshot must arrive
    while the review is still running, and each model_result must arrive as its
    own event rather than all of them appearing at the end.
    """
    import time as _time

    from tri_review.web.app import app

    class SlowStub(StubLLM):
        def invoke(self, messages):
            text = str(messages[0].content if messages else "")
            if "synthesizing" in text:
                return StubSynthLLM(self.model_name).invoke(messages)
            _time.sleep(0.25)
            return StubLLM.invoke(self, messages)

    app.state.llm_builder = lambda name: SlowStub(name)

    review_id = client.post(
        "/api/reviews", json={"url": "https://github.com/octocat/Hello-World/pull/42"}
    ).json()["id"]

    with client.stream("GET", f"/api/reviews/{review_id}/events") as response:
        body = "".join(response.iter_text())

    events = _sse_events(body)
    names = [name for name, _ in events]

    # Caught the review mid-flight, not as a finished replay.
    assert events[0][0] == "snapshot"
    assert events[0][1]["status"] in ("queued", "running")

    # Each model reported over the wire, individually.
    model_results = [payload for name, payload in events if name == "model_result"]
    assert len(model_results) == 3, f"expected 3 streamed results, got {names}"
    assert {r["result"]["model"] for r in model_results} == {
        "gpt-test-a",
        "claude-test-b",
        "gemini-test-c",
    }

    # And the report arrived after them, not bundled into the snapshot.
    assert "report" in names
    assert names.index("report") > names.index("model_result")
    assert names[-1] == "done"


def test_stream_of_an_unknown_review_is_a_404(client):
    assert client.get("/api/reviews/9999/events").status_code == 404


def test_stream_subscribes_before_reading_the_row(client, monkeypatch):
    """Subscription must happen before the row is read, not after.

    With the other order a review that completes in the gap publishes `done` to
    no subscriber, and the snapshot is built from the pre-completion row -- so
    the client holds an open stream that has already sent everything it will
    ever send. Asserted as call order rather than by racing the two, because a
    lost-event test can only fail by hanging.
    """
    from tri_review.web import jobs as jobs_mod
    from tri_review.web.routes import reviews as routes

    review_id = client.post(
        "/api/reviews", json={"url": "https://github.com/octocat/Hello-World/pull/42"}
    ).json()["id"]
    _await_terminal(client, review_id)

    calls: list[str] = []
    real_subscribe = jobs_mod.subscribe
    real_get = routes.db.get_review

    def traced_subscribe(rid):
        calls.append("subscribe")
        return real_subscribe(rid)

    def traced_get(rid):
        calls.append("get_review")
        return real_get(rid)

    monkeypatch.setattr(routes.jobs, "subscribe", traced_subscribe)
    monkeypatch.setattr(routes.db, "get_review", traced_get)

    with client.stream("GET", f"/api/reviews/{review_id}/events") as response:
        "".join(response.iter_text())

    assert calls[:2] == ["subscribe", "get_review"], (
        f"endpoint read the row before subscribing: {calls[:2]}"
    )


def test_stream_of_a_missing_review_does_not_leak_a_subscriber(client):
    """The 404 path subscribes first, so it has to unsubscribe on the way out."""
    from tri_review.web import jobs as jobs_mod

    before = len(jobs_mod._subscribers.get(9999, []))
    assert client.get("/api/reviews/9999/events").status_code == 404
    assert len(jobs_mod._subscribers.get(9999, [])) == before
