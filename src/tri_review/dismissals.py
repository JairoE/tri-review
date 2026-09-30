"""Findings a human has already judged and rejected, so they stop coming back.

A reviewer that re-raises a settled point is worse than annoying: the second
time it appears it carries the same "2 of 3 models agree" weight as the first,
so the reader has to re-derive the refutation or take it on faith. This file
is the memory that stops that. It happened here -- a finding was reported,
checked against every callable Gemini model, refuted, and the refutation
written into the code as a comment; the next run reported it again verbatim,
because nothing in the pipeline had anywhere to put "we already looked".

Deliberately not a fingerprint match. Keying a dismissal to a finding by
hashing its title is fragile in both directions -- wording drifts between runs
so real matches are missed, and a hash collision silently buries a live
finding. Instead the recorded claim and reason are handed to the synthesizer
as text and it does the matching, which is the thing a language model is
actually good at. The cost is that matching is not exact; the mitigation is
that it can only ever *downgrade* a finding and must say so in the report, so
a wrong match is visible to the reader rather than silent.

The three reviewers never see this file. They stay independent -- that
independence is the whole premise of triangulating them -- so a dismissal
changes how a finding is *reported*, never whether it is *found*.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from . import github

# Repo-local and version-controlled on purpose: a dismissal is a durable
# judgement about this codebase, so it belongs in review alongside the code it
# excuses, not in a user's cache directory where nobody else can see it.
DISMISSALS_PATH = Path(".tri-review") / "dismissed.toml"


@dataclass(frozen=True)
class Dismissal:
    """One finding a human looked at and rejected, with why."""

    claim: str
    reason: str
    file: str = ""
    date: str = ""

    def render(self) -> str:
        where = f" in `{self.file}`" if self.file else ""
        when = f" ({self.date})" if self.date else ""
        return f"- Claim{where}: {self.claim}\n  Rejected{when}: {self.reason}"


class MalformedDismissals(Exception):
    """The file exists but could not be read as a dismissal list.

    Raised rather than swallowed: a typo that silently drops every dismissal
    would let settled findings quietly return, which is the exact failure this
    module exists to prevent. Better to fail the run and say so.
    """


def parse(text: str | None, origin: str = "dismissal ledger") -> list[Dismissal]:
    """Parse ledger text into dismissals. None or blank means there are none.

    Separate from reading it so the caller decides where the bytes come from --
    a local checkout for a local review, and the PR's *base* ref for a remote
    one, which is what keeps a PR from dismissing its own findings.
    """
    if text is None or not text.strip():
        return []
    path = origin
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise MalformedDismissals(f"{path} is not readable TOML: {exc}") from exc

    entries = parsed.get("dismissed", [])
    if not isinstance(entries, list):
        raise MalformedDismissals(f"{path}: 'dismissed' must be a list of tables.")

    out: list[Dismissal] = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise MalformedDismissals(f"{path}: entry {i} is not a table.")
        claim, reason = entry.get("claim"), entry.get("reason")
        if not isinstance(claim, str) or not claim.strip():
            raise MalformedDismissals(f"{path}: entry {i} has no 'claim'.")
        if not isinstance(reason, str) or not reason.strip():
            # A dismissal without a reason is just suppression, and it would
            # give the synthesizer nothing to show the reader.
            raise MalformedDismissals(f"{path}: entry {i} has no 'reason'.")
        out.append(
            Dismissal(
                claim=claim.strip(),
                reason=reason.strip(),
                file=str(entry.get("file", "") or "").strip(),
                date=str(entry.get("date", "") or "").strip(),
            )
        )
    return out


def render(dismissals: list[Dismissal]) -> str:
    """Format dismissals for the synthesizer, or "" when there are none."""
    if not dismissals:
        return ""
    body = "\n".join(d.render() for d in dismissals)
    return (
        "A human has already reviewed and rejected the claims below on this "
        "codebase. If a finding in this run makes one of these claims again, "
        "do not promote it to Blocking on the strength of agreement -- say it "
        "was previously rejected, give the recorded reason, and treat it as "
        "Optional unless this diff provides new evidence the earlier rejection "
        "did not account for.\n\n" + body
    )


def trusted_ledger(repo: str | None, meta: dict) -> str | None:
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
        return github.fetch_file_content(identity, DISMISSALS_PATH.as_posix(), base)
    except Exception:  # noqa: BLE001 - same failure class as above
        return None
