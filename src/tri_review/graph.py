"""The LangGraph workflow: context -> three reviewers in parallel -> synthesizer."""

from __future__ import annotations

from pathlib import Path

from langgraph.graph import END, StateGraph

from . import config, context, github, nodes, providers
from .state import ReviewState


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
    diff = github.fetch_diff(pr_number, repo, excludes)

    if repo:
        head_ref = state.get("head_ref") or github.fetch_pr_meta(pr_number, repo)["head_sha"]
        reader = context.github_reader(repo, head_ref)
    else:
        head_ref = state.get("head_ref") or ""
        reader = context.filesystem_reader(Path.cwd())

    ctx = context.build_context(diff, reader=reader)
    return {
        "pr_number": pr_number,
        "repo": repo or "",
        "head_ref": head_ref,
        "payload": ctx.render(),
        "context": ctx,
    }


def build_review_graph(models: list[str] | None = None, llm_builder=providers.build_llm):
    """Compile the review graph. `models` defaults to the three configured slots."""
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
        workflow.add_node(node_name, nodes.make_review_node(model_name, llm_builder))
        workflow.add_edge("fetch_context", node_name)
        workflow.add_edge(node_name, "synthesize")

    workflow.add_edge("synthesize", END)
    return workflow.compile()
