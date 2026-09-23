"""The LangGraph workflow: context -> three reviewers in parallel -> synthesizer."""

from __future__ import annotations

from pathlib import Path

from langgraph.graph import END, StateGraph

from . import config, context, dismissals, github, nodes, providers
from .state import ReviewState


def _pr_meta_or_empty(pr_number: str, repo: str | None) -> dict:
    """PR metadata, or {} when it cannot be read.

    In --repo mode the head SHA is load-bearing and a failure must surface, so
    the caller fetches it directly. This is the cwd-mode path, where metadata
    is wanted only to locate a trusted dismissal ledger -- and losing that is
    survivable, because no ledger simply means nothing gets downgraded.
    """
    try:
        return github.fetch_pr_meta(pr_number, repo)
    except Exception:  # noqa: BLE001 - no PR, no auth, no network: all "no ledger"
        return {}


def _trusted_ledger(repo: str | None, meta: dict) -> str | None:
    """The dismissal ledger as of the PR's base commit, or None.

    Always the base, never the PR's own content and never the working tree.
    The Action checks out the PR head, so anything reachable on disk there is
    written by the author of the change under review -- and this file can only
    ever downgrade findings about that change.

    Fetched through the contents API rather than `git show` because the Action
    clones at fetch-depth 1: the base commit is not in the local object store,
    so a git read would fail and look exactly like "no dismissals recorded".

    Every failure returns None, which errs toward reporting more rather than
    less. There is no fallback to a less trusted source: a ledger that cannot
    be read from the base is treated as absent, not as whatever the PR says.
    """
    base = meta.get("base_sha")
    if not base:
        return None
    identity = repo
    if not identity:
        try:
            identity = github.parse_pr_url(meta.get("url") or "")[0]
        except Exception:  # noqa: BLE001 - cannot name the repo, cannot trust a ledger
            return None
    try:
        return github.fetch_file_content(identity, dismissals.DISMISSALS_PATH.as_posix(), base)
    except Exception:  # noqa: BLE001 - same failure class as above
        return None


def fetch_context_node(state: ReviewState) -> dict:
    """Resolve the PR, fetch its diff, and assemble the review payload.

    Two modes, decided by whether `repo` is on the state. With a repo, files are
    read from GitHub at the PR's head SHA; without one, from the checkout the
    process is sitting in. Resolving `head_ref` here rather than at the caller
    means no caller can forget it and silently review the default branch's files.
    `excludes` narrows the diff in either mode -- see `github.fetch_diff`.
    """
    repo = state.get("repo") or None
    excludes = state.get("excludes", ())
    pr_number = state.get("pr_number") or github.detect_pr(repo)
    diff = state.get("diff") or github.fetch_diff(pr_number, repo, excludes)

    if repo:
        meta = github.fetch_pr_meta(pr_number, repo)
        head_ref = state.get("head_ref") or meta["head_sha"]
        reader = context.github_reader(repo, head_ref)
    else:
        meta = _pr_meta_or_empty(pr_number, None)
        head_ref = state.get("head_ref") or ""
        reader = context.filesystem_reader(Path.cwd())

    # Same trusted source in both modes: the PR's base commit. See
    # _trusted_ledger for why the working tree is never consulted.
    ledger = _trusted_ledger(repo, meta)

    ctx = context.build_context(diff, reader=reader)
    return {
        "pr_number": pr_number,
        "repo": repo or "",
        "head_ref": head_ref,
        "dismissals": dismissals.parse(ledger),
        "diff": diff,
        "payload": ctx.render(),
        "context": ctx,
    }


def build_review_graph(
    models: list[str] | None = None,
    llm_builder=providers.build_llm,
    use_cache: bool = True,
):
    """Compile the review graph. `models` defaults to the three configured slots.

    `use_cache` off is what --fresh threads down to: it forces every reviewer to
    call its provider even when an identical payload was reviewed before.
    """
    models = models or [config.model_a(), config.model_b(), config.model_c()]

    workflow = StateGraph(ReviewState)
    workflow.add_node("fetch_context", fetch_context_node)
    # Bind the same builder the reviewers use, so tests can stub the synthesizer too.
    workflow.add_node("synthesize", lambda state: nodes.synthesize_node(state, llm_builder))

    workflow.set_entry_point("fetch_context")

    # Fan out: one node per model. LangGraph runs branches leaving a single node
    # concurrently, so wall time is the slowest model rather than their sum.
    for index, model_name in enumerate(models):
        node_name = f"review_{index}"
        workflow.add_node(
            node_name, nodes.make_review_node(model_name, llm_builder, use_cache)
        )
        workflow.add_edge("fetch_context", node_name)
        workflow.add_edge(node_name, "synthesize")

    workflow.add_edge("synthesize", END)
    return workflow.compile()
