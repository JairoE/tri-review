"""The LangGraph workflow: context -> three reviewers in parallel -> synthesizer."""

from __future__ import annotations

from langgraph.graph import END, StateGraph

from . import config, context, github, nodes
from .state import ReviewState


def fetch_context_node(state: ReviewState) -> dict:
    """Resolve the PR, fetch its diff, and assemble the review payload."""
    pr_number = state.get("pr_number") or github.detect_pr()
    diff = github.fetch_diff(pr_number)
    ctx = context.build_context(diff)
    return {"pr_number": pr_number, "payload": ctx.render(), "context": ctx}


def build_review_graph(models: list[str] | None = None, llm_builder=nodes.build_llm):
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
