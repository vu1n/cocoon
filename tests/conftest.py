from pathlib import Path

import pytest

from cocoon import agent_context, catalog


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path: Path, monkeypatch):
    """Per-test cache + auth + catalog dir under $COCOON_CACHE_DIR; never
    leaks into the real ~/.cache/cocoon. Also clears $COCOON_CATALOG_URL so
    every test sees the bundled dev catalog deterministically.

    Also clears `agent_context._load_bundled`'s lru_cache so a test that
    monkeypatches the bundled aggregate (or its loading path) doesn't
    serve stale content to a subsequent test. And clears
    `catalog._warned_min_score_values` so warn-once tests see a fresh
    state per test."""
    monkeypatch.setenv("COCOON_CACHE_DIR", str(tmp_path))
    monkeypatch.delenv("COCOON_CATALOG_URL", raising=False)
    agent_context._load_bundled.cache_clear()
    catalog._warned_min_score_values.clear()
