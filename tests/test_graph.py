import time

import pytest

from tri_review import graph as graph_mod
from tri_review.errors import InsufficientReviewsError
from tri_review.schema import Finding, ReviewOutput


class StubLLM:
    """Stands in for a provider. Optionally sleeps, to prove branches run concurrently."""

    def __init__(self, model_name, delay=0.0, raises=None):
        self.model_name = model_name
        self.delay = delay
        self.raises = raises

    def with_structured_output(self, _schema):
        return self

    def invoke(self, _messages):
        if self.delay:
            time.sleep(self.delay)
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


@pytest.fixture
def stub_context(monkeypatch):
    monkeypatch.setattr(graph_mod.github, "detect_pr", lambda repo=None: "1")
    monkeypatch.setattr(
        graph_mod.github,
        "fetch_diff",
        lambda pr, repo=None, exclude=(): "diff --git a/a.py b/a.py\n",
    )
    monkeypatch.setattr(
        graph_mod.context, "build_context", lambda diff, reader=None: _FakeCtx()
    )


class _FakeCtx:
    def render(self):
        return "payload"


def test_repo_mode_reads_files_at_the_pr_head_sha(monkeypatch):
    """The whole point of --repo: no checkout, and file bodies from the reviewed commit.

    Guards the plan's highest-severity silent failure -- reading contents off the
    default branch pairs the right diff with the wrong files and degrades every
    finding without raising anything.
    """
    calls = {}

    def fake_fetch_diff(pr, repo=None, exclude=()):
        calls["diff"] = (pr, repo)
        return "diff\n"

    def fake_github_reader(repo, ref):
        calls["reader"] = (repo, ref)
        return lambda rel: None

    monkeypatch.setattr(graph_mod.github, "fetch_diff", fake_fetch_diff)
    monkeypatch.setattr(
        graph_mod.github, "fetch_pr_meta", lambda pr, repo=None: {"head_sha": "a" * 40}
    )
    monkeypatch.setattr(graph_mod.context, "github_reader", fake_github_reader)
    monkeypatch.setattr(graph_mod.context, "build_context", lambda diff, reader=None: _FakeCtx())

    out = graph_mod.fetch_context_node({"pr_number": "7", "repo": "octocat/Hello-World"})

    assert calls["diff"] == ("7", "octocat/Hello-World")
    assert calls["reader"] == ("octocat/Hello-World", "a" * 40)
    assert out["head_ref"] == "a" * 40
    assert out["repo"] == "octocat/Hello-World"


def test_cwd_mode_uses_the_filesystem_reader(monkeypatch):
    """No repo on state means the old behaviour: read the checkout we are sitting in."""
    chosen = {}

    def fake_filesystem_reader(root):
        chosen["root"] = root
        return lambda rel: None

    monkeypatch.setattr(graph_mod.github, "fetch_diff", lambda pr, repo=None, exclude=(): "diff\n")
    monkeypatch.setattr(
        graph_mod.github,
        "fetch_pr_meta",
        lambda pr, repo=None: pytest.fail("cwd mode must not need PR metadata"),
    )
    monkeypatch.setattr(
        graph_mod.context,
        "github_reader",
        lambda repo, ref: pytest.fail("cwd mode must not use the GitHub reader"),
    )
    monkeypatch.setattr(graph_mod.context, "filesystem_reader", fake_filesystem_reader)
    monkeypatch.setattr(graph_mod.context, "build_context", lambda diff, reader=None: _FakeCtx())

    out = graph_mod.fetch_context_node({"pr_number": "7"})

    assert "root" in chosen
    assert out["repo"] == ""


def test_excludes_reach_fetch_diff_in_either_mode(monkeypatch):
    calls = {}

    monkeypatch.setattr(
        graph_mod.github,
        "fetch_diff",
        lambda pr, repo=None, exclude=(): calls.setdefault("exclude", exclude) or "diff\n",
    )
    monkeypatch.setattr(graph_mod.context, "filesystem_reader", lambda root: (lambda rel: None))
    monkeypatch.setattr(graph_mod.context, "build_context", lambda diff, reader=None: _FakeCtx())

    graph_mod.fetch_context_node({"pr_number": "7", "excludes": ("**/*.lock",)})

    assert calls["exclude"] == ("**/*.lock",)


def test_three_models_all_succeed(stub_context):
    app = graph_mod.build_review_graph(
        models=["m1", "m2", "m3"], llm_builder=lambda name: StubLLM(name)
    )
    out = app.invoke({"pr_number": "1", "results": []})
    assert len(out["results"]) == 3
    assert {r.model for r in out["results"]} == {"m1", "m2", "m3"}
    assert all(r.ok for r in out["results"])


def test_results_are_attributed_to_their_model(stub_context):
    app = graph_mod.build_review_graph(
        models=["m1", "m2", "m3"], llm_builder=lambda name: StubLLM(name)
    )
    out = app.invoke({"pr_number": "1", "results": []})
    for result in out["results"]:
        assert result.findings[0].title == f"issue seen by {result.model}"


def test_one_failing_model_leaves_the_other_two_intact(stub_context):
    def builder(name):
        if name == "m2":
            return StubLLM(name, raises=RuntimeError("no API key"))
        return StubLLM(name)

    app = graph_mod.build_review_graph(models=["m1", "m2", "m3"], llm_builder=builder)
    out = app.invoke({"pr_number": "1", "results": []})

    ok = [r for r in out["results"] if r.ok]
    failed = [r for r in out["results"] if not r.ok]
    assert len(ok) == 2
    assert len(failed) == 1
    assert failed[0].model == "m2"
    assert "no API key" in failed[0].error
    # Synthesis still ran on the surviving two.
    assert out["final_report"]


def test_fan_out_runs_concurrently(stub_context):
    """Three 0.4s models must finish well under their 1.2s serial sum."""
    app = graph_mod.build_review_graph(
        models=["m1", "m2", "m3"], llm_builder=lambda name: StubLLM(name, delay=0.4)
    )
    start = time.monotonic()
    app.invoke({"pr_number": "1", "results": []})
    elapsed = time.monotonic() - start
    assert elapsed < 0.9, f"fan-out appears serial: {elapsed:.2f}s"


def test_two_failures_abort_the_run(stub_context):
    def builder(name):
        if name in ("m2", "m3"):
            return StubLLM(name, raises=RuntimeError(f"{name} down"))
        return StubLLM(name)

    app = graph_mod.build_review_graph(models=["m1", "m2", "m3"], llm_builder=builder)
    with pytest.raises(InsufficientReviewsError, match="nothing to triangulate"):
        app.invoke({"pr_number": "1", "results": []})
