"""Exact-match cache for individual reviewer calls.

Keyed on a content hash, never on similarity. Two diffs that are 99% alike can
differ in exactly the line that introduces the bug, so "close enough" is not a
cache hit here -- it is a confident review of code that was never read. The key
covers everything that can change the answer: the model, the payload, the
reviewer prompt, and the output schema the model is being held to.

What this buys, concretely: when one provider flakes and the run exits 4, the
two reviews that did land are already paid for. Retrying re-calls only the model
that failed instead of buying all three again.

Only successful reviews are stored. Caching a failure would defeat the whole
purpose -- the retry has to actually retry.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from . import config
from .schema import ReviewResult

SCHEMA_VERSION = 1


def _directory() -> Path:
    return config.history_dir() / "reviews"


def key_for(model: str, payload: str, prompt: str, output_schema: str) -> str:
    """Hash everything that could change what the model returns.

    The prompt and schema are hashed rather than versioned by hand: editing
    REVIEW_PROMPT or adding a field to `Finding` invalidates every stored entry
    automatically, which is the behaviour you want and the one nobody remembers
    to trigger manually.
    """
    digest = hashlib.sha256()
    for part in (str(SCHEMA_VERSION), model, prompt, output_schema, payload):
        digest.update(part.encode("utf-8"))
        digest.update(b"\x00")  # domain separator, so fields cannot run together
    return digest.hexdigest()


def load(key: str) -> ReviewResult | None:
    """Return the stored review for `key`, or None on any miss.

    Every failure mode -- absent, corrupt, expired, written by a future version
    -- is a miss. A cache that can raise is worse than no cache at all.
    """
    path = _directory() / f"{key}.json"
    try:
        stat = path.stat()
    except OSError:
        return None

    max_age = config.cache_ttl_days() * 86400
    if max_age > 0 and (time.time() - stat.st_mtime) > max_age:
        return None

    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        result = ReviewResult.model_validate(obj["result"])
    except Exception:  # noqa: BLE001 - unreadable is indistinguishable from absent
        return None
    return result if result.ok else None


def save(key: str, result: ReviewResult) -> None:
    """Store a successful review. Never raises, never stores a failure."""
    if not result.ok:
        return
    path = _directory() / f"{key}.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".json.tmp")
        temp.write_text(
            json.dumps({"schema": SCHEMA_VERSION, "result": result.model_dump()}, indent=2),
            encoding="utf-8",
        )
        temp.replace(path)
    except OSError:
        return
