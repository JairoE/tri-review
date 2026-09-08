"""Per-PR review history: what the last run cost, found, and reported.

Two jobs, one file. The immediate one is the no-op gate -- re-running against a
head SHA that has already been reviewed should replay the stored report rather
than buy the same three opinions twice. The second is that structured findings
survive the run at all, which is what a later "resolved / still open / new"
comparison against a previous review needs. The rendered Markdown is kept
alongside them because replaying a report should not require re-synthesising it.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .schema import ReviewResult

SCHEMA_VERSION = 1

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass
class RunRecord:
    """One completed review, as stored on disk."""

    repo: str
    pr: str
    head_sha: str
    models: list[str]
    excludes: list[str]
    report: str
    results: list[ReviewResult] = field(default_factory=list)
    created_at: str = ""

    def to_json(self) -> str:
        return json.dumps(
            {
                "schema": SCHEMA_VERSION,
                "repo": self.repo,
                "pr": self.pr,
                "head_sha": self.head_sha,
                "models": self.models,
                "excludes": self.excludes,
                "created_at": self.created_at or _now(),
                "report": self.report,
                "results": [r.model_dump() for r in self.results],
            },
            indent=2,
        )

    @classmethod
    def from_obj(cls, obj: dict) -> "RunRecord | None":
        """Rebuild a record, or None if it is unreadable or from a future schema.

        Every failure here is a cache miss, never an error: a corrupt or
        unrecognised history file must cost the user a re-review, not a crash.
        """
        if not isinstance(obj, dict) or obj.get("schema") != SCHEMA_VERSION:
            return None
        try:
            return cls(
                repo=str(obj["repo"]),
                pr=str(obj["pr"]),
                head_sha=str(obj["head_sha"]),
                models=[str(m) for m in obj.get("models", [])],
                excludes=[str(e) for e in obj.get("excludes", [])],
                report=str(obj.get("report", "")),
                results=[ReviewResult.model_validate(r) for r in obj.get("results", [])],
                created_at=str(obj.get("created_at", "")),
            )
        except Exception:  # noqa: BLE001 - any malformed field is just a miss
            return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def path_for(repo: str, pr: str, directory: Path | None = None) -> Path:
    """Where this PR's history lives.

    `repo` reaches the filesystem as a name, so every separator and dot-segment
    is flattened rather than trusted -- the same reflex the diff-path guard in
    `github.is_repo_relative` applies to untrusted paths.
    """
    base = directory if directory is not None else config.history_dir()
    identity = f"{repo}:{pr}"
    slug = _UNSAFE.sub("-", f"{repo}-pr{pr}").strip("-.") or "unknown"
    # Flattening every separator is what keeps the name from escaping `base`,
    # but it also maps distinct identities onto one name -- `a/b` and `a-b` land
    # in the same file, and replaying one repository's report for another is a
    # wrong answer, not a cosmetic clash. The digest restores what flattening
    # destroyed without letting any of it reach the filesystem as structure.
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    return base / f"{slug}-{digest}.json"


def load(repo: str, pr: str, directory: Path | None = None) -> RunRecord | None:
    """Read this PR's last run, or None if there isn't a usable one."""
    path = path_for(repo, pr, directory)
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        record = RunRecord.from_obj(json.loads(raw))
    except json.JSONDecodeError:
        return None

    # Belt and braces with the digest in the filename: whatever the path says,
    # a record only answers for the identity it was written under.
    if record is not None and (record.repo != repo or record.pr != str(pr)):
        return None
    return record


def save(record: RunRecord, directory: Path | None = None) -> Path | None:
    """Persist a completed run. Returns the path, or None if it could not be written.

    Never raises. By the time this is called the models have already been paid
    for and the report is already on screen; a read-only cache directory must
    not turn a finished review into a failed run.
    """
    path = path_for(record.repo, record.pr, directory)
    record.created_at = record.created_at or _now()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename so an interrupted run cannot leave a truncated file
        # that the next run would read as a miss and silently re-review.
        temp = path.with_suffix(".json.tmp")
        temp.write_text(record.to_json(), encoding="utf-8")
        temp.replace(path)
    except OSError:
        return None
    return path


def stale_reason(
    record: RunRecord,
    head_sha: str,
    models: list[str],
    excludes: tuple[str, ...],
) -> str | None:
    """Why `record` cannot stand in for the run about to happen, or None if it can.

    All three inputs are load-bearing. A different head SHA is different code.
    A different panel is a different set of opinions -- replaying Sonnet's review
    when the user asked for Terra's would answer a question they did not ask. A
    different exclude set is a different slice of the PR. Any of them changing
    makes the stored report an answer to another question.
    """
    if not head_sha or record.head_sha != head_sha:
        return f"the PR has moved on ({_short(record.head_sha)} -> {_short(head_sha)})"
    if record.models != list(models):
        return (
            f"the panel changed ({', '.join(record.models) or 'none'} -> "
            f"{', '.join(models)})"
        )
    if record.excludes != list(excludes):
        return "the exclude patterns changed"
    if not record.report.strip():
        return "the stored run has no report"
    failed = [r.model for r in record.results if not r.ok]
    if failed:
        # The README promises that a re-run after a flake re-calls only the model
        # that failed. Report-level replay defeats that: it reprints the degraded
        # 2-of-3 report and the flaked model is never called again. Marking the
        # record stale hands the run to the per-model cache, which replays the
        # two that succeeded and buys only the one that did not -- which is the
        # documented behaviour.
        return f"the last run did not get a review from {', '.join(failed)}"
    return None


def _short(sha: str) -> str:
    return sha[:8] if sha else "unknown"
