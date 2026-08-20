"""FastAPI app: resolve a pasted PR URL, preview it, review it, watch it stream."""

from __future__ import annotations

import os

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from ..errors import (
    InsufficientReviewsError,
    PreflightError,
    PRNotFoundError,
    TriReviewError,
)
from .routes import reviews

# The CLI calls load_dotenv() at its entry point; without an equivalent here the
# provider keys are simply absent and every review fails at synthesis, which
# reads as a server bug rather than as missing configuration.
load_dotenv()

app = FastAPI(
    title="tri-review",
    description="Paste a GitHub PR URL, get three model reviews side by side.",
    version="0.1.0",
)

# The Next.js dev server runs on another port, so the browser calls this
# cross-origin. Local-first and single-user: the allowed origins are the dev
# server's, not a wildcard.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_STATUS = {
    PreflightError: 500,
    PRNotFoundError: 404,
    InsufficientReviewsError: 502,
}


@app.exception_handler(TriReviewError)
async def tri_review_error_handler(_request: Request, exc: TriReviewError):
    """Report the domain errors as JSON with a meaningful status, not a traceback."""
    return JSONResponse(
        status_code=_STATUS.get(type(exc), 500),
        content={"error": type(exc).__name__, "detail": str(exc)},
    )


@app.get("/api/health")
def health() -> dict:
    """Report readiness, including which provider keys are actually present.

    Nothing else in the package validates keys -- they resolve lazily inside the
    LangChain constructors -- so without this an unconfigured server looks
    healthy right up until it fails at synthesis.
    """
    keys = {
        "openai": bool(os.environ.get("OPENAI_API_KEY")),
        "anthropic": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "google": bool(
            os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
        ),
    }
    from .jobs import default_models

    return {
        "status": "ok",
        "models": default_models(),
        "provider_keys": keys,
        "keys_present": sum(keys.values()),
        # Two is the floor: the synthesizer refuses to triangulate one opinion.
        "ready": sum(keys.values()) >= 2,
    }


app.include_router(reviews.router)

# None means "use the real provider adapters". Tests swap in a stub so the whole
# API surface is exercisable with no keys and no network.
app.state.llm_builder = None
