# Plan: turn tri-review into a local-first full-stack app

## Context

`tri-review` today is a CLI that must be run from inside the checkout of the repo whose PR you want reviewed. It fans a PR out to three LLMs, synthesizes consensus, prints Markdown, and forgets everything. Two things limit it: you can only review a repo you have checked out and are `cd`'d into, and every review is discarded the moment the terminal scrolls.

The goal is a local web app: a dashboard where you pick any repo and PR you have access to, click Review, watch progress live, and keep a browsable history of past reviews.

**Decisions taken (from clarifying questions):** manual trigger (no webhooks), single-user local-first (no login, no tenancy), FastAPI + Next.js, server-side provider keys now with bring-your-own-key later.

The consequence worth stating up front: because this runs on your machine as you, **`gh` stays the GitHub client**. No OAuth flow, no token storage, no encryption-at-rest — the largest chunk of a typical "full-stack port" simply doesn't apply. What must change is that the core stops assuming a current working directory.

## Architecture decisions

- **Keep `gh`, make it repo-addressable.** `gh` accepts `--repo owner/name` on `pr list`/`pr view`/`pr diff`, and `gh api` reads file contents at a ref. That turns `github.py` from a rewrite into a parameterization. Keep `github.py` as the single GitHub seam so a REST implementation can drop in later if this ever goes multi-user.
- **Reuse the graph, with one seam added.** `graph.py:build_review_graph()` is already decoupled from the CLI and already exposes `.stream(..., stream_mode="updates")`, which maps one-to-one onto SSE events — `cli.py:150-163` consumes exactly that shape today, so the streaming contract is proven rather than hypothetical. The API drives the same compiled graph the CLI does — no second pipeline. The one thing that *must* change is the graph's entry node: `fetch_context_node` (`graph.py:11-16`) calls `github.detect_pr()`, `github.fetch_diff(pr)` and `context.build_context(diff)` with no repo, ref or reader, so it is hardcoded to cwd-mode. It reads `repo`/`head_ref` off state instead (see Task 2); `build_review_graph`'s signature is unchanged.
- **Pydantic models are the API contract.** `schema.py` (`Finding`, `ReviewResult`) is already Pydantic v2, so it serializes directly into FastAPI responses and OpenAPI, and the Next.js client types generate from that. One source of truth for the shape of a finding.
- **In-process jobs, no queue.** Single user, one machine: a thread plus an `asyncio.Queue` per review is sufficient. Celery/Redis would be infrastructure with no payer. The graph is sync, so it runs in a worker thread and pushes events back to the event loop.
- **SQLite via SQLModel.** Store the review row with results and report; no separate findings table until something needs to query across reviews.
- **One package, optional extra.** Backend lives at `src/tri_review/web/` under a `web` optional-dependency group, so `pip install -e .` still gives a pure CLI with no FastAPI dependency.

## Reuse surface (do not rewrite these)

| Existing | Role in the web app |
|---|---|
| `graph.py:build_review_graph()` | Driven directly by the job runner; `.stream()` feeds SSE. Its entry node `fetch_context_node` is the one part that changes — see Task 2 |
| `schema.py` `Finding` / `ReviewResult` | API response models and generated TS types |
| `nodes.py` `review_with`, `synthesize_node`, `provider_of`, `PROVIDER_PREFIXES` | Unchanged |
| `context.py` `parse_changed_files`, budget logic, `ReviewContext.render()` | Unchanged — only the file-read loop is filesystem-bound |
| `errors.py` | Exit codes become HTTP statuses: `PreflightError`→500, `PRNotFoundError`→404, `InsufficientReviewsError`→502 |
| `config.py` | Model/budget/timeout resolution; gains one seam for future BYOK |

## Tasks

### Phase 1 — Make the core repo-addressable (CLI stays green)

**Task 1: Repo-parameterized GitHub client**

Add an optional `repo: str | None` ("owner/name") to `fetch_diff` and `detect_pr`, passing `--repo` to `gh` when present. Add `list_repos()`, `list_pull_requests(repo)`, `fetch_pr_meta(pr, repo)`, and `fetch_file_content(repo, path, ref)` (via `gh api repos/{repo}/contents/{path}?ref=`, base64-decoded). Split `preflight()` so the "inside a git repo" check only applies to cwd-mode.

**The head SHA is not optional.** `fetch_pr_meta` must return `headRefOid` (`gh pr view <n> --repo <r> --json number,title,author,updatedAt,headRefOid`). Without it the GitHub reader in Task 2 has no ref and would read file contents off the default branch — the right diff paired with the wrong file bodies, which is precisely the failure `README.md:65-70` warns about, except server-side and silent, with no `gh pr checkout` available to correct it. This corrupts review quality rather than erroring, so it is the one thing in Phase 1 that must not be deferred.

**`path` is untrusted.** It comes from a diff header written by whoever opened the PR, and it is interpolated into an API path requested with the user's own `gh` credentials. `fetch_file_content` must reject any path with a `..` segment or a leading `/` before building the URL (see Task 2, which puts the guard at the shared boundary).

- **Acceptance:** `fetch_diff("10856", repo="octocat/Hello-World")` works from an unrelated directory; `list_pull_requests` returns number/title/author/updatedAt; `fetch_pr_meta` returns a 40-char `headRefOid`; `fetch_file_content` raises rather than requesting when given `../../etc/passwd`; existing cwd behaviour unchanged when `repo` is None.
- **Verification:** `pytest tests/test_github.py`; manually fetch a PR diff from `/tmp`.
- **Files:** `src/tri_review/github.py`, `tests/test_github.py`
- **Depends on:** none · **Size:** M

**Task 2: Pluggable context reader**

Change `build_context(diff, reader=..., budget=...)` to take a `Callable[[str], str | None]` instead of reading the filesystem inline. Provide `filesystem_reader(root)` (current behaviour, keeps `root=` working) and `github_reader(repo, ref)` using Task 1's fetcher. Add `--repo` to the CLI.

**Thread it through the graph, or `--repo` never reaches a review.** Add `repo: str` and `head_ref: str` to `ReviewState` (`state.py:11`), and rewrite `fetch_context_node` (`graph.py:11-16`) to read them: pass `repo` to `detect_pr`/`fetch_diff`, resolve `head_ref` via Task 1's `fetch_pr_meta` when it is absent, and pick `github_reader(repo, head_ref)` when `repo` is set, `filesystem_reader(Path.cwd())` when it is not. Skipping this yields a build where the CLI accepts `--repo` and the web app can browse any repo, but every actual review still runs against the server's cwd.

**Move the path-traversal guard to the boundary.** `_is_inside` (`context.py:59-70`) is filesystem-specific — it resolves a `Path` against a root — and stops protecting anything once the reader is pluggable, even though `README.md:137` sells the refusal as a product property. Reject repo-escaping paths (any `..` segment, any leading `/`) once, before the reader is called, so both readers inherit it; keep `filesystem_reader`'s symlink check on top, and keep `ctx.rejected` as the reporting channel so the CLI output and the future API surface it identically.

- **Acceptance:** local reader reproduces today's behaviour byte-for-byte; github reader populates `files` for a remote PR at the PR's head SHA, not the default branch; a diff header of `+++ b/../../etc/passwd` lands in `rejected` under *both* readers and issues no request; unreadable files still land in `missing` rather than raising; `build_review_graph` runs a full review against a repo the process is not `cd`'d into.
- **Verification:** `pytest tests/test_context.py tests/test_graph.py`; `tri-review --repo octocat/Hello-World --pr 10856 --dry-run` from `/tmp`.
- **Files:** `src/tri_review/context.py`, `src/tri_review/graph.py`, `src/tri_review/state.py`, `src/tri_review/cli.py`, `tests/test_context.py`, `tests/test_graph.py`
- **Depends on:** Task 1 · **Size:** M

**Task 2b: Extract a reusable context preview**

Pull the dry-run path out of `cli._run` (`cli.py:106-111`) into `context.preview_context(pr_number, repo=None) -> ReviewContext` and have the CLI call it. Add a `ContextSummary` Pydantic model to `schema.py` (files included, dropped, missing, rejected, estimated tokens, diff overflow) — `ReviewContext` is a plain dataclass (`context.py:11`), so "Pydantic models are the API contract" currently covers `schema.py` only, and Task 4's `/preview` endpoint needs a wire shape.

- **Acceptance:** `--dry-run` output is unchanged; `preview_context` makes zero provider calls; `ContextSummary.model_validate(ctx)` round-trips a real context.
- **Verification:** `pytest tests/test_context.py`; `tri-review --pr <n> --dry-run` before and after produce identical output.
- **Files:** `src/tri_review/context.py`, `src/tri_review/schema.py`, `src/tri_review/cli.py`, `tests/test_context.py`
- **Depends on:** Task 2 · **Size:** S

> **Checkpoint A:** all 72 existing tests green, CLI works both cwd-style and `--repo`-style, and a `--repo` review of a PR from an unrelated directory reads file contents at that PR's head SHA. The CLI is now strictly more capable and nothing web-facing exists yet — a safe place to stop.

### Phase 2 — Backend

**Task 3: FastAPI skeleton + browse endpoints**

`src/tri_review/web/app.py` with `GET /api/health`, `GET /api/repos`, `GET /api/repos/{owner}/{repo}/pulls`. Add a `web` optional-dependency group (fastapi, uvicorn, sqlmodel, sse-starlette). Map `TriReviewError` subclasses to HTTP statuses via an exception handler.

**Declare the browse handlers `def`, not `async def`.** They call `github._run`, a blocking `subprocess.run` with a 60s ceiling (`github.py:15,25`). A plain `def` handler runs in Starlette's threadpool; an `async def` one stalls the entire event loop for up to a minute on a slow `gh repo list`.

**Report key presence in `/api/health`, and call `load_dotenv()` at startup.** Nothing in the package reads or validates provider keys — they resolve lazily inside the LangChain constructors, so a missing key becomes a per-model `ReviewResult.error` (`nodes.py:57-66`). That is right for the CLI, but in the web app an unconfigured server would run the whole path and fail at synthesis with `InsufficientReviewsError` → 502, which reads as a server bug rather than missing configuration. `load_dotenv()` is currently called only at `cli.py:48`; without an equivalent at app startup the keys are simply absent.

- **Acceptance:** `uvicorn tri_review.web.app:app` serves all three; `/docs` lists them; a `gh` failure returns a JSON error, not a traceback; `/api/health` names which of the three provider keys are present; browse endpoints stay responsive while a slow `gh` call is in flight.
- **Verification:** `curl` each endpoint; `pytest tests/test_web_browse.py` using FastAPI `TestClient` with `gh` stubbed.
- **Files:** `src/tri_review/web/app.py`, `src/tri_review/web/routes/repos.py`, `pyproject.toml`, `tests/test_web_browse.py`
- **Depends on:** Task 1 · **Size:** M

**Task 4: Persistence + review jobs**

SQLModel `Review` table (id, repo, pr_number, models, status, created_at, finished_at, report_md, results_json, error). `POST /api/reviews` creates a row and starts a background thread running the graph; `GET /api/reviews/{id}` returns status and report; `GET /api/reviews` lists history. Add `POST /api/reviews/preview` returning the `ReviewContext` summary **without calling any model** — the `--dry-run` logic as an endpoint, so the UI can show context size before spending money.

- **Acceptance:** POST returns 202 with an id immediately; status goes queued→running→succeeded/failed; a review survives a server restart; preview makes zero provider calls.
- **Verification:** `pytest tests/test_web_reviews.py` with a stubbed `llm_builder`; curl POST then poll GET.
- **Files:** `src/tri_review/web/db.py`, `src/tri_review/web/jobs.py`, `src/tri_review/web/routes/reviews.py`, `tests/test_web_reviews.py`
- **Depends on:** Tasks 2, 3 · **Size:** L — split jobs from routes if it grows past ~5 files

**Task 5: Live progress over SSE**

`GET /api/reviews/{id}/events` streaming via `sse-starlette`. The worker thread pushes each `stream_mode="updates"` chunk onto an `asyncio.Queue` with `loop.call_soon_threadsafe`; the endpoint drains it. Event types mirror graph nodes: `context`, `model_result`, `report`, `done`, `error`. Late subscribers replay from the DB so a browser refresh mid-review doesn't hang.

- **Acceptance:** `curl -N` shows per-model results as they land, not batched at the end; refreshing mid-review resumes; a completed review's stream closes immediately with the final report.
- **Verification:** `curl -N` against a real review; a test asserting event ordering with stubbed models.
- **Files:** `src/tri_review/web/jobs.py`, `src/tri_review/web/routes/reviews.py`, `tests/test_web_sse.py`
- **Depends on:** Task 4 · **Size:** M

> **Checkpoint B:** a full review is drivable end-to-end with `curl` alone — no frontend. This is the highest-risk phase; confirm streaming and persistence here before any UI work.

### Phase 3 — Frontend

**Task 6: Next.js app + PR picker**

Next.js (App Router, TypeScript, Tailwind) in `web/`. Generate API types from the FastAPI OpenAPI schema (`openapi-typescript`). Home page lists repos, drills into open PRs, each with a Review button. Dev proxy to the FastAPI port.

- **Acceptance:** repos and PRs load from the live backend; types are generated, not hand-written; loading and empty states render.
- **Verification:** `npm run dev`, click through to a PR list.
- **Files:** `web/app/page.tsx`, `web/lib/api.ts`, `web/package.json`
- **Depends on:** Task 3 · **Size:** M

**Task 7: Review page with live progress**

`/reviews/[id]` opens the SSE stream: context summary, per-model status as each lands, then the synthesized report rendered with `react-markdown`. Preview-before-run: hitting Review shows the context summary and estimated tokens from `/preview` with a confirm step, so a review is never started by accident.

- **Acceptance:** progress appears incrementally; the report renders as formatted Markdown; a failed model shows its error without blocking the report; refresh mid-review reconnects.
- **Verification:** run a real review in the browser against a PR with a known bug.
- **Files:** `web/app/reviews/[id]/page.tsx`, `web/components/*`
- **Depends on:** Tasks 5, 6 · **Size:** M

**Task 8: History, errors, and a BYOK-ready settings page**

History list of past reviews with status and repo/PR. Friendly error surfaces for the `PreflightError`/`InsufficientReviewsError` cases. A settings page reading current model slots from the backend, with provider key fields present but disabled and labelled "server-configured" — the seam for BYOK without building it now.

Also update `README.md`: it currently documents "run from the repo root with that PR's branch checked out" as a core usage rule (`README.md:54-70`), which the port removes for `--repo` mode.

- **Acceptance:** history persists across restarts; the two known error states render as guidance, not stack traces; settings reflects live config; the README describes both cwd-mode and `--repo`-mode accurately.
- **Verification:** full pass — pick a PR, preview, review, refresh, revisit from history.
- **Files:** `web/app/history/page.tsx`, `web/app/settings/page.tsx`, `src/tri_review/web/routes/settings.py`, `README.md`
- **Depends on:** Task 7 · **Size:** M

> **Checkpoint C:** end-to-end from browser against a real PR, history survives restart, CLI still green.

## Risks

| Risk | Impact | Mitigation |
|---|---|---|
| `gh repo list` slow or noisy on large accounts | Med | Paginate and add a filter box; cache the list briefly |
| Reviews cost real money and the UI makes them one click | High | `/preview` + explicit confirm step (Tasks 4 and 7); this is why preview is in the plan rather than a nice-to-have |
| Sync graph blocking the event loop | High | Run in a worker thread; never call `.invoke()`/`.stream()` on the loop |
| Blocking `gh` subprocess in the browse endpoints | Med | Same root cause, different site: `github._run` blocks for up to 60s. Keep browse handlers plain `def` so Starlette threadpools them (Task 3) |
| GitHub reader reads the wrong ref | High | Always read at the PR's `headRefOid` (Task 1). Reading the default branch pairs the right diff with the wrong file bodies and degrades findings silently |
| Untrusted diff paths reaching `gh api` | Med | Reject `..` segments and absolute paths at the reader boundary before building the URL (Task 2) |
| SSE buffered by a dev proxy | Med | Verify with `curl -N` before blaming the frontend; disable buffering in the Next dev proxy config |
| A review hangs on a slow provider | Med | Per-model timeout already exists in `config.model_timeout()`; surface it as a job timeout and add a cancel endpoint |
| Scope creep toward multi-user | Med | Keep `github.py` the only GitHub seam; avoid embedding "current user" assumptions in the DB schema |

## Verification

- **Regression:** `pytest` — the existing 72 tests must stay green through Phases 1–2; the CLI is the guard that the core wasn't broken by the web port.
- **Backend alone:** `curl` POST a review, poll GET, and `curl -N` the SSE stream — the whole product works headless before any UI exists.
- **End-to-end:** with all three provider keys set, review a PR containing a known defect and confirm it reaches Consensus in the browser, then reload the page and revisit it from history.
- **Offline test suite:** all new backend tests stub `llm_builder` and `gh`; the suite must stay runnable with no API keys and no network, as it is today.

## Known gaps carried forward

Recorded from the pre-port readiness review, not addressed by the tasks above:

- **No CI, lint, or typecheck.** No `.github/workflows`, no `Makefile`, no pre-commit; `pyproject.toml` configures pytest and nothing else, and dev deps are `pytest>=8.0` alone. This port roughly triples the surface (FastAPI + SQLite + Next.js) with no automated gate. Worth a follow-on task before Phase 3.
- **Untested paths the web app will lean on.** `_run`, `_stream_graph`, `--dry-run` and `--output` (`cli.py:97-206`) have no coverage, and `build_llm`'s three real provider branches (`providers.py:35-48`) are never exercised — only the unrecognized-model `ValueError`. Task 2b makes the preview path testable; the streaming path stays covered only via `test_graph.py`.
- **No cancellation seam.** The graph compiles without a checkpointer (`graph.py:39`) and `review_with` catches `Exception` broadly (`nodes.py:65`), so the cancel endpoint in the risk table has nothing to hook into yet. Fine for a single-user local app; revisit if job timeouts become real.

## Out of scope (deliberately)

Webhooks and auto-review on PR open; posting results back to the PR as a comment; authentication and multi-tenancy; billing; BYOK key entry (seam only). Each is a natural follow-on, and none is needed for the local-first app described here.
