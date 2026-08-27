.PHONY: install test

install:
	uv sync --extra dev

test:
	uv run pytest -q
