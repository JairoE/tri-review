.PHONY: install test lock-action

install:
	uv sync --extra dev

test:
	uv run pytest -q

# Regenerate requirements-lock.txt from uv.lock. Run this after any dependency
# change (a `uv add`/`uv lock`, or a `uv.lock` bump from Dependabot) -- the
# GitHub Action installs against this file, not uv.lock directly, so a stale
# copy here lets the Action's pip install silently drift onto newer releases
# the test suite has never run against.
lock-action:
	uv export --no-hashes --no-dev --no-emit-project --format requirements-txt -o requirements-lock.txt
