"""SQLite persistence for reviews.

One table. Results and the report are stored as JSON/text on the row rather than
normalized into a findings table -- nothing queries across reviews, and a review
is always read whole.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from sqlmodel import Field, Session, SQLModel, create_engine, select

# Status values a review moves through. `failed` still carries whatever
# per-model results arrived before the failure, so a run that could not be
# synthesized is still readable side by side.
QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Review(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    repo: str
    pr_number: str
    pr_title: str = ""
    pr_author: str = ""
    pr_url: str = ""
    head_sha: str = ""
    models: str = ""  # comma-separated, in panel order
    status: str = QUEUED
    created_at: datetime = Field(default_factory=_utcnow)
    finished_at: datetime | None = None
    # JSON list of ReviewResult.model_dump(), one per model.
    results_json: str = "[]"
    # JSON ContextSummary.model_dump(), or "" before the context is built.
    context_json: str = ""
    report_md: str = ""
    error: str = ""


def db_path() -> Path:
    """Where the SQLite file lives. Overridable so tests get a temp file."""
    configured = os.environ.get("TRI_REVIEW_DB")
    if configured:
        return Path(configured)
    return Path.home() / ".tri-review" / "reviews.db"


_engine = None


def engine():
    """Lazily create the engine so TRI_REVIEW_DB can be set after import."""
    global _engine
    if _engine is None:
        path = db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: the job runner writes from a worker thread
        # while the event loop reads from another.
        _engine = create_engine(
            f"sqlite:///{path}", connect_args={"check_same_thread": False}
        )
        SQLModel.metadata.create_all(_engine)
    return _engine


def reset_engine() -> None:
    """Drop the cached engine. Tests call this after repointing TRI_REVIEW_DB."""
    global _engine
    if _engine is not None:
        _engine.dispose()
    _engine = None


def session() -> Session:
    return Session(engine())


def get_review(review_id: int) -> Review | None:
    with session() as db:
        return db.get(Review, review_id)


def list_reviews(limit: int = 50) -> list[Review]:
    with session() as db:
        rows = db.exec(
            select(Review).order_by(Review.id.desc()).limit(limit)
        ).all()
        return list(rows)


def create_review(**fields) -> Review:
    with session() as db:
        review = Review(**fields)
        db.add(review)
        db.commit()
        db.refresh(review)
        return review


def update_review(review_id: int, **fields) -> None:
    with session() as db:
        review = db.get(Review, review_id)
        if review is None:
            return
        for key, value in fields.items():
            setattr(review, key, value)
        db.add(review)
        db.commit()
