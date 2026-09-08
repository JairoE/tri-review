"""Test-wide isolation for anything that writes outside the repository.

`review_with` and the history sidecar both persist to a user cache directory by
default, which is correct for the product and wrong for a test suite: without
this, one test's cached success is replayed into the next test that happens to
use the same model name and payload, and running `pytest` quietly writes into
the developer's real ~/.cache/tri-review.
"""

import pytest


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("TRI_REVIEW_HISTORY_DIR", str(tmp_path / "tri-review"))
