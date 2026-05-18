from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path: Path, monkeypatch):
    """Per-test cache + auth + catalog dir under $COCOON_CACHE_DIR; never
    leaks into the real ~/.cache/cocoon. Also clears $COCOON_CATALOG_URL so
    every test sees the bundled dev catalog deterministically."""
    monkeypatch.setenv("COCOON_CACHE_DIR", str(tmp_path))
    monkeypatch.delenv("COCOON_CATALOG_URL", raising=False)
