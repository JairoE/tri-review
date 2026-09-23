import time

import pytest

from tri_review import graph as graph_mod
from tri_review.errors import InsufficientReviewsError
from tri_review.schema import Finding, ReviewOutput


class _StubRaw:
    usage_metadata = None


class StubLLM:
    """Stands in for a provider. Optionally sleeps, to prove branches run concurrently."""

    def __init__(self, model_name, delay=0.0, raises=None):
        self.model_name = model_name
        self.delay = delay
        self.raises = raises

    def with_structured_output(self, _schema, include_raw=False):
        return self

    def invoke(self, _messages):
        if self.delay:
            time.sleep(self.delay)
        if self.raises:
            raise self.raises
        parsed = ReviewOutput(
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
        return {"raw": _StubRaw(), "parsed": parsed, "parsing_error": None}


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
    # No real `gh pr view` for the trusted-ledger base ref. Left unstubbed it
    # is a live network round-trip inside every graph test, which quietly
    # inflates the fan-out timing assertion below.
    monkeypatch.setattr(
        graph_mod.github, "fetch_pr_meta", lambda pr, repo=None: {"base_sha": ""}
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
    """No repo on state means the old behaviour: read the checkout we are sitting in.

    PR metadata may be consulted -- the dismissal ledger needs a trusted base
    ref even here, because the Action runs in this mode against a PR-head
    checkout. What must not change is where *file contents* come from: the
    local checkout, never the GitHub reader.
    """
    chosen = {}

    def fake_filesystem_reader(root):
        chosen["root"] = root
        return lambda rel: None

    monkeypatch.setattr(graph_mod.github, "fetch_diff", lambda pr, repo=None, exclude=(): "diff\n")
    monkeypatch.setattr(
        graph_mod.github, "fetch_pr_meta", lambda pr, repo=None: {"base_sha": ""}
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


def test_a_retry_after_two_flakes_only_re_calls_the_models_that_failed(stub_context):
    """The headline claim of the reviewer cache, end to end through the graph.

    Two of three providers drop out, so the run exits 4 with nothing to
    triangulate -- but the review that did land was already paid for. Re-running
    must buy only the two that failed.
    """
    calls = []

    def builder(model_name):
        calls.append(model_name)
        if model_name in ("flaky-one", "flaky-two"):
            return StubLLM(model_name, raises=RuntimeError("connection error"))
        return StubLLM(model_name)

    models = ["steady", "flaky-one", "flaky-two"]

    for _ in range(2):
        app = graph_mod.build_review_graph(models=models, llm_builder=builder)
        with pytest.raises(InsufficientReviewsError):
            app.invoke({"pr_number": "1", "repo": "", "results": []})

    assert calls.count("steady") == 1
    assert calls.count("flaky-one") == 2
    assert calls.count("flaky-two") == 2


def test_a_prefetched_diff_is_not_fetched_again(monkeypatch):
    """The triage gate already paid for the diff; the graph must not re-ask."""
    fetched = []

    monkeypatch.setattr(graph_mod.github, "detect_pr", lambda repo=None: "1")
    monkeypatch.setattr(
        graph_mod.github,
        "fetch_diff",
        lambda pr, repo=None, exclude=(): fetched.append(pr) or "diff\n",
    )
    monkeypatch.setattr(
        graph_mod.context, "build_context", lambda diff, reader=None: _FakeCtx()
    )

    state = graph_mod.fetch_context_node(
        {"pr_number": "1", "repo": "", "diff": "diff --git a/already.py b/already.py\n"}
    )

    assert fetched == []
    assert state["diff"].startswith("diff --git a/already.py")


def test_the_ledger_is_read_at_the_base_ref_not_the_pr_head(monkeypatch):
    """A PR must not be able to dismiss findings about itself.

    The ledger downgrades findings, so reading it from the PR's own commits
    would let an author silence review of a change by adding an entry to it in
    that same change. It is read at the merge base instead, which is code that
    is already on the trunk.
    """
    from tri_review import context, dismissals, github

    asked = {}

    monkeypatch.setattr(github, "fetch_diff", lambda *a, **k: "diff --git a/x b/x\n+++ b/x\n")
    monkeypatch.setattr(
        github, "fetch_pr_meta",
        lambda *a, **k: {"head_sha": "head" * 10, "base_sha": "base" * 10},
    )
    monkeypatch.setattr(context, "github_reader", lambda *a, **k: (lambda p: None))

    def fake_fetch_file_content(repo, path, ref):
        asked["path"], asked["ref"] = path, ref
        return '[[dismissed]]\nclaim = "c"\nreason = "r"\n'

    monkeypatch.setattr(github, "fetch_file_content", fake_fetch_file_content)

    out = graph_mod.fetch_context_node({"pr_number": "1", "repo": "o/n"})

    assert asked["ref"] == "base" * 10, "ledger must not be read at the PR head"
    assert asked["path"] == str(dismissals.DISMISSALS_PATH)
    assert [d.claim for d in out["dismissals"]] == ["c"]


def test_a_pr_without_a_base_ref_simply_has_no_ledger(monkeypatch):
    """Missing base ref means no dismissals, never a fall back to head."""
    from tri_review import context, github

    monkeypatch.setattr(github, "fetch_diff", lambda *a, **k: "diff --git a/x b/x\n+++ b/x\n")
    monkeypatch.setattr(
        github, "fetch_pr_meta", lambda *a, **k: {"head_sha": "h" * 40, "base_sha": ""}
    )
    monkeypatch.setattr(context, "github_reader", lambda *a, **k: (lambda p: None))

    def explode(*a, **k):  # pragma: no cover - must not be reached
        raise AssertionError("should not read a ledger without a trusted ref")

    monkeypatch.setattr(github, "fetch_file_content", explode)

    assert graph_mod.fetch_context_node({"pr_number": "1", "repo": "o/n"})["dismissals"] == []


def test_cwd_mode_reads_the_ledger_at_the_base_not_the_working_tree(monkeypatch):
    """The Action runs in cwd mode against a checkout of the PR head.

    So the ledger on disk there is written by the author of the change under
    review. It has to come from the base commit in this mode too, or the trust
    boundary only exists in the mode the Action does not use. It is fetched
    through the contents API rather than git because the Action clones at
    fetch-depth 1 and the base commit is not in the local object store.
    """
    monkeypatch.setattr(graph_mod.github, "fetch_diff", lambda *a, **k: "diff\n")
    monkeypatch.setattr(
        graph_mod.github,
        "fetch_pr_meta",
        lambda pr, repo=None: {
            "base_sha": "base" * 10,
            "url": "https://github.com/o/n/pull/7",
        },
    )
    monkeypatch.setattr(graph_mod.context, "filesystem_reader", lambda root: (lambda p: None))
    monkeypatch.setattr(graph_mod.context, "build_context", lambda diff, reader=None: _FakeCtx())
    seen = {}

    def fake_contents(repo, path, ref):
        seen.update(repo=repo, path=path, ref=ref)
        return '[[dismissed]]\nclaim = "c"\nreason = "r"\n'

    monkeypatch.setattr(graph_mod.github, "fetch_file_content", fake_contents)

    out = graph_mod.fetch_context_node({"pr_number": "7"})

    assert seen["ref"] == "base" * 10, "ledger must not be read at the PR head"
    assert seen["repo"] == "o/n"
    assert "\\" not in seen["path"], "API path must be POSIX, not Windows-separated"
    assert [d.claim for d in out["dismissals"]] == ["c"]


def test_a_failed_metadata_lookup_yields_no_ledger_not_a_local_one(monkeypatch):
    """An auth, network or rate-limit failure must not reopen the hole.

    Falling back to the checkout on any error would mean a transient GitHub
    failure silently promotes the PR's own ledger to a trusted one. No ledger
    is the safe direction: nothing gets downgraded.
    """
    monkeypatch.setattr(graph_mod.github, "fetch_diff", lambda *a, **k: "diff\n")

    def boom(pr, repo=None):
        raise RuntimeError("gh: rate limit exceeded")

    monkeypatch.setattr(graph_mod.github, "fetch_pr_meta", boom)
    monkeypatch.setattr(graph_mod.context, "filesystem_reader", lambda root: (lambda p: None))
    monkeypatch.setattr(graph_mod.context, "build_context", lambda diff, reader=None: _FakeCtx())
    monkeypatch.setattr(
        graph_mod.github,
        "fetch_file_content",
        lambda *a, **k: pytest.fail("must not look for a ledger without a trusted base"),
    )

    assert graph_mod.fetch_context_node({"pr_number": "7"})["dismissals"] == []


def test_a_failed_ledger_fetch_yields_no_ledger(monkeypatch):
    """Same rule one layer down: unreadable at the base means absent."""
    monkeypatch.setattr(graph_mod.github, "fetch_diff", lambda *a, **k: "diff\n")
    monkeypatch.setattr(
        graph_mod.github,
        "fetch_pr_meta",
        lambda pr, repo=None: {"base_sha": "b" * 40, "url": "https://github.com/o/n/pull/7"},
    )
    monkeypatch.setattr(graph_mod.context, "filesystem_reader", lambda root: (lambda p: None))
    monkeypatch.setattr(graph_mod.context, "build_context", lambda diff, reader=None: _FakeCtx())

    def boom(*a, **k):
        raise RuntimeError("contents API down")

    monkeypatch.setattr(graph_mod.github, "fetch_file_content", boom)

    assert graph_mod.fetch_context_node({"pr_number": "7"})["dismissals"] == []
