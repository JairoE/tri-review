"""Resolve, preview, start and watch reviews.

The handlers that shell out to `gh` are declared `def`, not `async def`. They
call `github._run`, a blocking subprocess with a 60s ceiling; on the event loop
that would stall every other request for up to a minute, while a plain `def`
handler is run in Starlette's threadpool.
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from ... import config, context, github
from ...errors import PRNotFoundError
from ...providers import PROVIDER_PREFIXES, provider_of
from ...schema import ContextSummary, ReviewResult, render_findings_md
from .. import db, jobs

router = APIRouter(prefix="/api")


class PRRef(BaseModel):
    """A pull request named either by URL, or by repo + number."""

    url: str | None = None
    repo: str | None = None
    pr_number: str | None = None

    def resolve(self) -> tuple[str, str]:
        if self.url:
            return github.parse_pr_url(self.url)
        if self.repo and self.pr_number:
            return self.repo, str(self.pr_number)
        raise PRNotFoundError(
            "Give either a pull request URL, or a repo and a pr_number."
        )


class ReviewRequest(PRRef):
    models: list[str] = Field(default_factory=list)


def _resolve_models(selected: list[str]) -> list[str]:
    """Pick the panel, rejecting a typo before the PR is fetched and money spent."""
    if not selected:
        return jobs.default_models()

    # The same model twice cannot corroborate itself.
    models = list(dict.fromkeys(selected))
    if len(models) < 2:
        raise PRNotFoundError(
            f"Need at least 2 distinct models to triangulate, got {len(models)}."
        )
    unknown = [m for m in models if provider_of(m) is None]
    if unknown:
        supported = ", ".join(
            f"{p} ({', '.join(x + '*' for x in prefixes)})"
            for p, prefixes in PROVIDER_PREFIXES.items()
        )
        raise PRNotFoundError(
            f"Unrecognized model ID(s): {', '.join(unknown)}. Supported: {supported}."
        )
    return models


@router.post("/pulls/resolve")
def resolve_pull_request(ref: PRRef) -> dict:
    """Turn a pasted PR URL into the PR's identity, without fetching its diff.

    Cheap enough to run as the user pastes, so a typo is caught before the
    heavier preview call.
    """
    repo, pr_number = ref.resolve()
    github.preflight(repo)
    meta = github.fetch_pr_meta(pr_number, repo)
    return {"repo": repo, **meta}


@router.post("/reviews/preview")
def preview(ref: PRRef) -> dict:
    """Show what would be sent to the models. Makes no provider calls."""
    repo, pr_number = ref.resolve()
    github.preflight(repo)
    ctx = context.preview_context(pr_number, repo)
    return {
        "repo": repo,
        "pr_number": pr_number,
        "context": ContextSummary.of(ctx).model_dump(),
        "models": jobs.default_models(),
        "token_budget": config.token_budget(),
    }


def _serialize(review: db.Review) -> dict:
    """One review as the UI reads it: three rendered columns plus the report."""
    raw = json.loads(review.results_json or "[]")
    results = [ReviewResult.model_validate(item) for item in raw]
    panel = [m for m in review.models.split(",") if m]
    by_model = {r.model: r for r in results}

    # Keyed by panel order, not arrival order, so the columns stay put as
    # results land rather than reshuffling under the reader.
    columns = [
        {
            "model": name,
            "status": (
                "pending" if name not in by_model
                else "ok" if by_model[name].ok
                else "failed"
            ),
            "findings": [f.model_dump() for f in by_model[name].findings] if name in by_model else [],
            "error": by_model[name].error if name in by_model else None,
            "markdown": render_findings_md(by_model[name]) if name in by_model else "",
        }
        for name in panel
    ]

    return {
        "id": review.id,
        "repo": review.repo,
        "pr_number": review.pr_number,
        "pr_title": review.pr_title,
        "pr_author": review.pr_author,
        "pr_url": review.pr_url,
        "head_sha": review.head_sha,
        "models": panel,
        "status": review.status,
        "created_at": review.created_at.isoformat() if review.created_at else None,
        "finished_at": review.finished_at.isoformat() if review.finished_at else None,
        "columns": columns,
        "context": json.loads(review.context_json) if review.context_json else None,
        "report_md": review.report_md,
        "error": review.error,
    }


@router.post("/reviews", status_code=202)
async def create_review(req: ReviewRequest, request: Request) -> dict:
    """Start a review and return its id immediately."""
    models = _resolve_models(req.models)

    # gh calls block, so keep them off the loop even though this handler is
    # async (it has to be, to capture the running loop for the worker thread).
    def gather() -> tuple[str, str, dict]:
        repo, pr_number = req.resolve()
        github.preflight(repo)
        return repo, pr_number, github.fetch_pr_meta(pr_number, repo)

    repo, pr_number, meta = await asyncio.to_thread(gather)

    review = db.create_review(
        repo=repo,
        pr_number=pr_number,
        pr_title=meta["title"],
        pr_author=meta["author"],
        pr_url=meta["url"],
        head_sha=meta["head_sha"],
        models=",".join(models),
        status=db.QUEUED,
    )

    jobs.start(review.id, asyncio.get_running_loop(), llm_builder=request.app.state.llm_builder)
    return {"id": review.id, "status": review.status, "repo": repo, "pr_number": pr_number}


@router.get("/reviews")
def list_reviews() -> list[dict]:
    return [_serialize(r) for r in db.list_reviews()]


@router.get("/reviews/{review_id}")
def get_review(review_id: int) -> dict:
    review = db.get_review(review_id)
    if review is None:
        raise PRNotFoundError(f"No review with id {review_id}.")
    return _serialize(review)


@router.get("/reviews/{review_id}/events")
async def review_events(review_id: int, request: Request) -> EventSourceResponse:
    """Stream a review's progress, replaying what already happened first.

    Replay comes from the row rather than a buffer, so a browser that refreshes
    mid-review — or opens a finished one — sees the same thing either way and
    never hangs waiting for events it already missed.
    """
    review = db.get_review(review_id)
    if review is None:
        raise PRNotFoundError(f"No review with id {review_id}.")

    # Subscribe before replaying, so an event landing between the two is queued
    # rather than lost in the gap.
    queue = jobs.subscribe(review_id)

    async def stream():
        try:
            snapshot = _serialize(review)
            yield {"event": "snapshot", "data": json.dumps(snapshot)}

            if snapshot["status"] in (db.SUCCEEDED, db.FAILED):
                yield {"event": "done", "data": json.dumps(
                    {"status": snapshot["status"], "error": snapshot["error"]}
                )}
                return

            while True:
                if await request.is_disconnected():
                    return
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    # A comment keeps intermediaries from closing an idle
                    # connection while a slow model is still thinking.
                    yield {"event": "ping", "data": "{}"}
                    continue

                yield {"event": event["type"], "data": json.dumps(event)}
                if event["type"] == "done":
                    return
        finally:
            jobs.unsubscribe(review_id, queue)

    return EventSourceResponse(stream())
