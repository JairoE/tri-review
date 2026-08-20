.PHONY: install dev api web test

API_PORT ?= 8000

install:
	uv sync --extra web --extra dev
	cd web && npm install

# Both servers, backgrounded, and both stopped when you Ctrl-C this.
dev:
	@trap 'kill 0' EXIT INT TERM; \
	uv run uvicorn tri_review.web.app:app --port $(API_PORT) & \
	(cd web && npm run dev) & \
	wait

api:
	uv run uvicorn tri_review.web.app:app --port $(API_PORT) --reload

web:
	cd web && npm run dev

test:
	uv run pytest -q
	cd web && npx tsc --noEmit
