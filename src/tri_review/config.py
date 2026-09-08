"""Runtime configuration. Every value is overridable via a TRI_REVIEW_* env var."""

from __future__ import annotations

import os
from pathlib import Path

# Model IDs, one per reviewer slot. Deliberately mid-tier rather than each
# provider's flagship: gpt-5.6-terra, claude-sonnet-5, and gemini-3.7-flash
# trade some capability for materially lower per-token cost, which matters
# for a tool that reviews a full PR diff (often 100k+ tokens) on every run.
# Override any slot with TRI_REVIEW_MODEL_A/B/C, or via --model, for a
# flagship panel when the extra cost is worth it.
#
# Provider model lists move fast; if a default 404s or is retired, override
# it with the corresponding env var rather than assuming the code is wrong.
#
# None of these accept a custom `temperature`: the current OpenAI and Anthropic
# flagships reject sampling parameters outright, so tri-review never sends one.
DEFAULT_MODEL_A = "gpt-5.6-terra"
DEFAULT_MODEL_B = "claude-sonnet-5"
DEFAULT_MODEL_C = "gemini-3.7-flash"

# Paths that do not earn a place in the review payload. Two tiers, because
# "not worth spending tokens on" and "grounds for skipping the PR entirely" are
# different claims and conflating them is how a review goes missing.
#
# Tier one: prose and generated artifacts that carry no decision. A PR touching
# only these has nothing to triangulate, so it is safe to skip outright.
SKIP_ELIGIBLE_EXCLUDES = (
    "**/*.md",
    "**/*.mdx",
    "**/*.rst",
    "docs/**",
    "**/LICENSE",
    "**/CHANGELOG*",
    "**/*.snap",
    "**/__snapshots__/**",
    "**/*.png",
    "**/*.jpg",
    "**/*.jpeg",
    "**/*.gif",
    "**/*.ico",
    "**/*.pdf",
)

# Tier two: machine-written files that nobody hand-edits, but which do record a
# decision. Reviewing a resolved dependency graph line by line is waste when
# there is real code in the PR -- and a PR that changes *only* a lockfile is the
# exact shape of a malicious or accidental dependency bump, which is the last
# thing that should pass unreviewed. So these are dropped from the payload but
# never count as grounds for skipping the run.
#
# Deliberately absent from both tiers: `requirements.txt`, `CODEOWNERS`, and
# anything else that looks like documentation but changes behaviour or access.
DEPENDENCY_EXCLUDES = (
    "**/*.lock",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "go.sum",
    "requirements-lock.txt",
)

DEFAULT_EXCLUDES = SKIP_ELIGIBLE_EXCLUDES + DEPENDENCY_EXCLUDES

# Rough char-per-token ratio used to estimate context size without a tokenizer.
CHARS_PER_TOKEN = 4


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from None


def model_a() -> str:
    return os.environ.get("TRI_REVIEW_MODEL_A") or DEFAULT_MODEL_A


def model_b() -> str:
    return os.environ.get("TRI_REVIEW_MODEL_B") or DEFAULT_MODEL_B


def model_c() -> str:
    return os.environ.get("TRI_REVIEW_MODEL_C") or DEFAULT_MODEL_C


def synthesizer_model() -> str:
    """Model that cross-references the reviews. Defaults to slot A."""
    return os.environ.get("TRI_REVIEW_SYNTHESIZER") or model_a()


def token_budget() -> int:
    """Max estimated tokens for diff + file context combined."""
    return _env_int("TRI_REVIEW_TOKEN_BUDGET", 100_000)


def model_timeout() -> int:
    """Per-model wall-clock timeout in seconds."""
    return _env_int("TRI_REVIEW_TIMEOUT", 120)


def estimate_tokens(text: str) -> int:
    return len(text) // CHARS_PER_TOKEN


def _env_excludes() -> tuple[str, ...] | None:
    raw = os.environ.get("TRI_REVIEW_EXCLUDE")
    if raw is None or raw.strip() == "":
        return None
    parts = [p.strip() for p in raw.replace(",", "\n").splitlines()]
    return tuple(p for p in parts if p) or None


def default_excludes() -> tuple[str, ...]:
    """Patterns kept out of the review payload, or TRI_REVIEW_EXCLUDE instead.

    Replaces rather than extends, mirroring how TRI_REVIEW_MODEL_A replaces a
    slot: this is the knob for "my standing default set", and a user who wants
    the built-ins plus their own can pass the extras as --exclude, which always
    adds. Accepts a comma- or newline-separated list.
    """
    return _env_excludes() or DEFAULT_EXCLUDES


def skip_eligible_excludes() -> tuple[str, ...]:
    """Patterns whose presence alone justifies skipping the whole review.

    A subset of `default_excludes`, which is the point: everything here is also
    dropped from the payload, but not everything dropped from the payload
    belongs here. A user-supplied TRI_REVIEW_EXCLUDE is taken at face value in
    both roles -- someone naming their own patterns is stating what they do not
    want reviewed, and it is not this function's place to overrule them.
    """
    return _env_excludes() or SKIP_ELIGIBLE_EXCLUDES


def history_dir() -> Path:
    """Where per-PR review history is kept.

    A user cache directory rather than the repository, so it works identically
    in --repo mode (where there is no checkout to write into) and never needs a
    .gitignore entry. CI runners get a fresh one each run, which is correct: a
    new push is a new head SHA and would miss the cache anyway.
    """
    override = os.environ.get("TRI_REVIEW_HISTORY_DIR")
    if override and override.strip():
        return Path(override.strip()).expanduser()
    base = os.environ.get("XDG_CACHE_HOME") or "~/.cache"
    return Path(base).expanduser() / "tri-review"


def cache_ttl_days() -> int:
    """How long a cached reviewer call stays usable. 0 disables expiry."""
    return _env_int("TRI_REVIEW_CACHE_TTL_DAYS", 14)


def triage_model() -> str:
    """Model asked whether a diff changes behaviour at all.

    Defaults to slot C, the cheapest of the three. Triage is a gate in front of
    a much larger spend, so it only makes sense while it stays a rounding error
    against the review it might avoid.
    """
    return os.environ.get("TRI_REVIEW_TRIAGE_MODEL") or model_c()


def triage_enabled() -> bool:
    """Whether the behaviour-change gate runs when no flag says otherwise.

    Off by default. It is the only gate that costs money and the only one that
    can be wrong, so turning it on is a decision the user makes rather than one
    they discover after a review they wanted was skipped.
    """
    raw = (os.environ.get("TRI_REVIEW_TRIAGE") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")
