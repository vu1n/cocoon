from pathlib import Path

import pytest

from cocoon import catalog


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path: Path, monkeypatch):
    """Per-test cache + auth + catalog dir under $COCOON_CACHE_DIR; never
    leaks into the real ~/.cache/cocoon. Stubs catalog._fetch_remote so
    tests get the bundled dev catalog (5 APIs) deterministically without
    hitting the network — load_catalog still goes through its normal
    cache-then-fetch flow, just with the dev catalog as the "remote"
    response.

    Also clears `catalog._warned_min_score_values` so warn-once tests
    see a fresh state per test."""
    monkeypatch.setenv("COCOON_CACHE_DIR", str(tmp_path))
    monkeypatch.delenv("COCOON_CATALOG_URL", raising=False)
    # Stub the network fetch with the bundled dev catalog so every test
    # sees a deterministic 5-API corpus offline.
    monkeypatch.setattr(catalog, "_fetch_remote", lambda _url: catalog._load_dev_catalog())
    catalog._warned_min_score_values.clear()
