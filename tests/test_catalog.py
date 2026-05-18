from pathlib import Path

import pytest

from cocoon import catalog
from cocoon.errors import CapabilityNotFound


def test_load_catalog_returns_dev_fallback() -> None:
    data = catalog.load_catalog()
    apis = {entry["api"] for entry in data}
    assert {"linear", "slack", "github", "stripe", "hackernews"} <= apis


def test_load_catalog_writes_cache(tmp_path: Path) -> None:
    catalog.load_catalog()
    cache_file = tmp_path / "catalog" / catalog.CACHE_FILE
    assert cache_file.exists()


def test_find_capability_returns_relevant_top_match() -> None:
    results = catalog.find_capability("create a linear issue")
    assert results
    top = results[0]
    assert top.api == "linear"
    assert top.tool == "issues.create"
    assert "title" in top.params_schema


def test_find_capability_respects_limit() -> None:
    assert len(catalog.find_capability("list", limit=2)) <= 2


def test_find_capability_empty_query_returns_nothing() -> None:
    assert catalog.find_capability("") == []
    assert catalog.find_capability("   ") == []


def test_find_capability_unmatched_query_returns_nothing() -> None:
    assert catalog.find_capability("xyzzy-no-such-word") == []


def test_describe_capability_returns_full_record() -> None:
    cap = catalog.describe_capability("slack", "chat.postMessage")
    assert cap.api == "slack"
    assert cap.tool == "chat.postMessage"
    assert "channel" in cap.params_schema


def test_describe_capability_raises_for_unknown() -> None:
    with pytest.raises(CapabilityNotFound):
        catalog.describe_capability("linear", "nonexistent.tool")


def test_list_apis_filter_matches_name_or_description() -> None:
    all_apis = catalog.list_apis()
    assert len(all_apis) >= 5
    filtered = catalog.list_apis("payments")
    assert {s.api for s in filtered} == {"stripe"}


def test_list_apis_endpoint_count_matches() -> None:
    for summary in catalog.list_apis():
        assert summary.endpoint_count > 0


def test_refresh_catalog_rewrites_cache(tmp_path: Path) -> None:
    cache_file = tmp_path / "catalog" / catalog.CACHE_FILE
    catalog.load_catalog()
    first_mtime = cache_file.stat().st_mtime_ns
    # Simulate stale cache by stomping it.
    cache_file.write_text("[]")
    catalog.refresh_catalog()
    assert cache_file.read_text() != "[]"
    assert cache_file.stat().st_mtime_ns >= first_mtime
