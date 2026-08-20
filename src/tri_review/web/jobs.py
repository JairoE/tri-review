"""Running a review in the background and broadcasting its progress.

The graph is synchronous, so it runs in a worker thread and hands events back to
the event loop with `call_soon_threadsafe`. One thread and one queue per review
is enough for a single-user local app; a queue would be infrastructure with no
payer.

Every event is written to the database *before* it is broadcast, so a browser
that connects late replays the same history from the row rather than missing it.
"""

from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime, timezone

from .. import config
from ..errors import TriReviewError
from ..schema import ContextSummary, ReviewResult
from . import db

# Live subscribers, keyed by review id. Guarded by _lock because the worker
# thread publishes while the event loop subscribes and unsubscribes.
_subscribers: dict[int, list[asyncio.Queue]] = {}
_lock = threading.Lock()


def default_models() -> list[str]:
    return [config.model_a(), config.model_b(), config.model_c()]


def subscribe(review_id: int) -> asyncio.Queue:
    queue: asyncio.Queue = asyncio.Queue()
    with _lock:
        _subscribers.setdefault(review_id, []).append(queue)
    return queue


def unsubscribe(review_id: int, queue: asyncio.Queue) -> None:
    with _lock:
        queues = _subscribers.get(review_id)
        if not queues:
            return
        if queue in queues:
            queues.remove(queue)
        if not queues:
            _subscribers.pop(review_id, None)


def _publish(review_id: int, loop: asyncio.AbstractEventLoop, event: dict) -> None:
    """Hand an event to every live subscriber, from the worker thread."""
    with _lock:
        queues = list(_subscribers.get(review_id, []))
    for queue in queues:
        try:
            loop.call_soon_threadsafe(queue.put_nowait, event)
        except RuntimeError:
            pass  # loop already closed; the DB still has the event


def is_running(review_id: int) -> bool:
    review = db.get_review(review_id)
    return review is not None and review.status in (db.QUEUED, db.RUNNING)


def start(review_id: int, loop: asyncio.AbstractEventLoop, llm_builder=None) -> threading.Thread:
    """Run a review in a daemon thread and return it (tests join on it)."""
    thread = threading.Thread(
        target=_run, args=(review_id, loop, llm_builder), daemon=True, name=f"review-{review_id}"
    )
    thread.start()
    return thread


def _run(review_id: int, loop: asyncio.AbstractEventLoop, llm_builder=None) -> None:
    from ..graph import build_review_graph

    review = db.get_review(review_id)
    if review is None:
        return

    models = [m for m in review.models.split(",") if m]
    results: list[ReviewResult] = []

    db.update_review(review_id, status=db.RUNNING)
    _publish(review_id, loop, {"type": "status", "status": db.RUNNING})

    try:
        kwargs = {"models": models}
        if llm_builder is not None:
            kwargs["llm_builder"] = llm_builder
        app = build_review_graph(**kwargs)

        initial = {
            "pr_number": review.pr_number,
            "repo": review.repo,
            "results": [],
        }
        for chunk in app.stream(initial, stream_mode="updates"):
            for node, update in chunk.items():
                if node == "fetch_context":
                    _on_context(review_id, loop, update)
                elif node.startswith("review_"):
                    _on_model_results(review_id, loop, update, results)
                elif node == "synthesize":
                    _on_report(review_id, loop, update)

    except TriReviewError as exc:
        # Reviews that already landed are on the row. A run that could not be
        # synthesized is still worth reading side by side, so this is a failed
        # status carrying results rather than a discarded run.
        _finish(review_id, loop, db.FAILED, f"{exc}")
        return
    except Exception as exc:  # noqa: BLE001 - a worker thread must not die silently
        _finish(review_id, loop, db.FAILED, f"{type(exc).__name__}: {exc}")
        return

    _finish(review_id, loop, db.SUCCEEDED, "")


def _on_context(review_id: int, loop, update: dict) -> None:
    ctx = update.get("context")
    if ctx is None:
        return
    summary = ContextSummary.of(ctx)
    db.update_review(
        review_id,
        context_json=summary.model_dump_json(),
        head_sha=update.get("head_ref") or "",
    )
    _publish(review_id, loop, {"type": "context", "context": summary.model_dump()})


def _on_model_results(review_id: int, loop, update: dict, results: list[ReviewResult]) -> None:
    # In "updates" mode each chunk carries only that node's own contribution,
    # so this is exactly one result per reviewer node.
    for result in update.get("results", []):
        results.append(result)
        db.update_review(
            review_id, results_json=json.dumps([r.model_dump() for r in results])
        )
        _publish(review_id, loop, {"type": "model_result", "result": result.model_dump()})


def _on_report(review_id: int, loop, update: dict) -> None:
    report = update.get("final_report") or ""
    db.update_review(review_id, report_md=report)
    _publish(review_id, loop, {"type": "report", "report_md": report})


def _finish(review_id: int, loop, status: str, error: str) -> None:
    db.update_review(
        review_id,
        status=status,
        error=error,
        finished_at=datetime.now(timezone.utc),
    )
    _publish(review_id, loop, {"type": "done", "status": status, "error": error})
