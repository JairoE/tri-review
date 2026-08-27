"""requirements-lock.txt must track uv.lock exactly.

action.yml installs from requirements-lock.txt, not uv.lock, so the GitHub
Action gets the exact provider SDK versions this repo's own test suite runs
against (see Makefile's `lock-action` target). A `uv add`/`uv lock` that
updates uv.lock without also regenerating requirements-lock.txt would leave
the Action free to drift onto an untested release again -- this is the same
class of gap that once let it install a langchain-anthropic release which
renamed a routine Anthropic connection error into a class the report had
never seen before.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _uv_lock_versions() -> dict[str, str]:
    data = tomllib.loads((ROOT / "uv.lock").read_text())
    return {pkg["name"]: pkg["version"] for pkg in data["package"]}


def _requirements_lock_versions() -> dict[str, str]:
    versions = {}
    for line in (ROOT / "requirements-lock.txt").read_text().splitlines():
        match = re.match(r"^([A-Za-z0-9_.-]+)==([^\s;]+)", line)
        if match:
            versions[match.group(1).lower()] = match.group(2)
    return versions


def test_requirements_lock_matches_uv_lock():
    uv_versions = _uv_lock_versions()
    req_versions = _requirements_lock_versions()

    assert req_versions, "requirements-lock.txt has no pinned packages -- regenerate it"

    stale = {
        name: (pinned, uv_versions[name])
        for name, pinned in req_versions.items()
        if name in uv_versions and uv_versions[name] != pinned
    }
    assert not stale, (
        f"requirements-lock.txt is out of sync with uv.lock: {stale} "
        "(name: (requirements-lock.txt version, uv.lock version)) -- run `make lock-action`"
    )


def test_requirements_lock_excludes_the_project_itself():
    """`-e .` (an editable self-install) is invalid inside a pip constraints file.

    `tri-review` legitimately appears in `# via tri-review` annotation comments
    (it's a dependent, not a pin), so this checks actual requirement lines --
    parsed pins and any `-e`/editable line -- rather than the raw text.
    """
    lines = (ROOT / "requirements-lock.txt").read_text().splitlines()
    assert not any(line.strip().startswith(("-e ", "-e\t")) for line in lines)
    assert "tri-review" not in _requirements_lock_versions()
