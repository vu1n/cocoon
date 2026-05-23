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
    """BM25 should surface a hackernews stories endpoint for a 'top stories' query.
    Once the bundled aggregate ships, the exact ranked tool may shift with
    upstream's descriptions; assert on the API + an obviously-relevant
    tool family rather than a specific endpoint."""
    results = catalog.find_capability("get the top stories on hacker news")
    assert results
    top = results[0]
    assert top.api == "hackernews"
    assert top.tool.startswith("stories.")


def test_find_capability_respects_limit() -> None:
    assert len(catalog.find_capability("list", limit=2)) <= 2


def test_find_capability_empty_query_returns_nothing() -> None:
    assert catalog.find_capability("") == []
    assert catalog.find_capability("   ") == []


def test_find_capability_unmatched_query_returns_nothing() -> None:
    """Truly nonsense tokens shouldn't hit anything; ordinary 'xyzzy' style
    words can still match common stopwords in 96 APIs' descriptions."""
    assert catalog.find_capability("qqqqxxxxzzzz9999abcd-nothinglikethis") == []


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
    # Bundled corpus has multiple payments-related APIs (stripe, mercury, …);
    # just assert stripe is there, not that it's the only match.
    filtered = {s.api for s in catalog.list_apis("payments")}
    assert "stripe" in filtered


def test_list_apis_includes_endpoint_counts() -> None:
    """Every API gets a numeric endpoint count. 0 is legitimate — some
    bundled CLIs ship without a tools-manifest yet."""
    for summary in catalog.list_apis():
        assert summary.endpoint_count >= 0


def test_agent_context_cache_overrides_endpoints_for_installed_api(tmp_path: Path) -> None:
    """When a per-API agent-context cache exists, its endpoints replace the
    dev catalog's hand-curated ones — discovery uses real data for any
    installed CLI."""
    import json
    (tmp_path / "agent-context").mkdir()
    (tmp_path / "agent-context" / "hackernews.json").write_text(json.dumps({
        "auth": {"mode": "none"},
        "commands": [
            {"name": "stories", "use": "stories", "short": "...",
             "subcommands": [
                 {"name": "top", "use": "top",
                  "short": "Get the current top stories",
                  "annotations": {"pp:endpoint": "stories.top"},
                  "flags": [{"name": "limit", "type": "int"}]},
             ]},
        ],
    }))
    results = catalog.find_capability("top stories on hacker news")
    tools = [r.tool for r in results]
    assert "stories.top" in tools


def test_agent_context_cache_invisible_for_uncatalogued_api(tmp_path: Path) -> None:
    """A stray agent-context cache for an API not in the catalog stays
    invisible — the catalog is the corpus boundary."""
    import json
    (tmp_path / "agent-context").mkdir()
    (tmp_path / "agent-context" / "ghost.json").write_text(json.dumps({
        "commands": [{"name": "x", "use": "x", "short": "...",
                      "annotations": {"pp:endpoint": "x.y"}}],
    }))
    results = catalog.find_capability("x")
    assert all(r.api != "ghost" for r in results)


def test_auth_type_prefers_agent_context_over_dev_catalog(tmp_path: Path) -> None:
    """The local binary's self-reported auth mode is the source of truth."""
    import json
    (tmp_path / "agent-context").mkdir()
    # Dev catalog says hackernews is auth_type=none. Override to api_key here.
    (tmp_path / "agent-context" / "hackernews.json").write_text(json.dumps({
        "auth": {"mode": "api_key"}, "commands": [],
    }))
    assert catalog.auth_type("hackernews") == "api_key"


def test_find_hides_entries_without_install_module(tmp_path: Path, monkeypatch) -> None:
    """RC5: entries the agent couldn't actually call (no install_module)
    shouldn't appear in find. The agent has every reason to trust a find
    result; surfacing uninstallable entries triggers fan-out and wasted
    round-trips. With the installability filter, they stay hidden."""
    import json
    (tmp_path / "agent-context").mkdir()
    # Forge a local cache for an API that ISN'T in the bundle and ISN'T
    # in the dev catalog. It has rich endpoints, but no install_module
    # can be derived (no catalog entry → no path → no module). find
    # should not surface its endpoints despite the local cache existing.
    (tmp_path / "agent-context" / "ghost-installable.json").write_text(json.dumps({
        "commands": [{"name": "x", "use": "x", "short": "...",
                      "annotations": {"pp:endpoint": "x.y"}}],
    }))
    results = catalog.find_capability("x")
    assert all(r.api != "ghost-installable" for r in results)


def test_describe_does_not_hide_uninstallable() -> None:
    """describe is a direct lookup by api+tool — if the agent has the name
    they can ask about it. The filter applies only to discovery surfaces.
    github is in the dev catalog with hand-curated endpoints but no
    install_module, and isn't in the bundled aggregate; find/list hide
    it, but describe still surfaces its hand-curated tools."""
    cap = catalog.describe_capability("github", "issues.create")
    assert cap.api == "github"
    assert cap.tool == "issues.create"


def test_find_surfaces_score_per_result() -> None:
    """Each find result carries its BM25 score. Agents that reason about
    confidence can ignore weak matches even when no global threshold is set."""
    results = catalog.find_capability("hacker news top stories")
    assert results
    assert all(r.score > 0 for r in results)
    # results are sorted by descending score
    assert all(results[i].score >= results[i + 1].score for i in range(len(results) - 1))


def test_find_min_score_floor_filters_low_matches(monkeypatch) -> None:
    """COCOON_FIND_MIN_SCORE drops matches below the floor. The postmortem's
    pointhound false-positive is the canonical case this knob blunts."""
    baseline = catalog.find_capability("foo bar baz random words")
    # All baseline results have some score, however small.
    if not baseline:
        return  # corpus matches nothing for this query; threshold doesn't apply
    # Setting the floor above the top match should clear the result set.
    top_score = max(r.score for r in baseline)
    monkeypatch.setenv("COCOON_FIND_MIN_SCORE", str(top_score + 1.0))
    assert catalog.find_capability("foo bar baz random words") == []


def test_min_score_bad_value_warns_and_falls_back(monkeypatch, capsys) -> None:
    """A typo'd COCOON_FIND_MIN_SCORE was silently 0.0 before — the user
    would assume the knob doesn't work. Now emits a stderr warning."""
    monkeypatch.setenv("COCOON_FIND_MIN_SCORE", "oops-not-a-number")
    catalog.find_capability("anything")
    assert "COCOON_FIND_MIN_SCORE" in capsys.readouterr().err


def test_min_score_bad_value_warns_only_once_per_value(monkeypatch, capsys) -> None:
    """For a long-lived MCP server, a typo'd env var would spam stderr on
    every find_capability call. The warn-once guard keeps the warning
    informative without becoming noise."""
    monkeypatch.setenv("COCOON_FIND_MIN_SCORE", "typo-once")
    catalog.find_capability("query one")
    catalog.find_capability("query two")
    catalog.find_capability("query three")
    err = capsys.readouterr().err
    # Exactly one warning line for this value.
    assert err.count("COCOON_FIND_MIN_SCORE") == 1


def test_installable_skip_count_returns_int() -> None:
    """doctor reports this so the silent-failure surface (RC5) is visible
    at health-check time."""
    count = catalog.installable_skip_count()
    assert isinstance(count, int) and count >= 0


def test_dev_catalog_inherits_install_module_from_bundled() -> None:
    """The 'dev wins entirely' merge was too aggressive — stripe in dev
    had no install_module so the bundled-derived path was discarded.
    Field-level overlay lets dev override what it has (endpoints) while
    inheriting what it doesn't (install_module from bundled)."""
    # stripe is both in dev catalog (no install_module) and in bundled
    # (path → derived install_module). After merge it should be installable.
    stripe_entries = [s for s in catalog.list_apis() if s.api == "stripe"]
    assert stripe_entries, "stripe should be visible in list (inherited install_module)"


def test_capability_carries_auth_status() -> None:
    """auth_status surfaces on every find result so the agent can decide
    whether to attempt the call or defer for setup. hackernews is bundled
    with auth_type=none, so its capabilities are "none"; linear is
    api_key, so without a token file present it's "required"."""
    hn = catalog.find_capability("top stories on hacker news")
    assert hn and hn[0].auth_status == "none"
    linear = catalog.find_capability("create a linear issue")
    assert linear and linear[0].auth_status == "required"


def test_find_sorts_ready_before_required() -> None:
    """Ready capabilities (auth_status none/configured) sort before gated
    ones, regardless of raw BM25 score. With the bundled corpus, an
    auth-less HN endpoint should rank ahead of an auth-gated one on a
    query that matches both."""
    results = catalog.find_capability("create", limit=5)
    if not any(r.auth_status == "required" for r in results):
        return  # nothing to compare; query didn't surface gated APIs
    # Find the first required and the last ready; ready must come first.
    first_required = next(i for i, r in enumerate(results) if r.auth_status == "required")
    last_ready = max((i for i, r in enumerate(results)
                      if r.auth_status in ("none", "configured")), default=-1)
    assert last_ready < first_required


def test_find_ready_only_hides_gated() -> None:
    results = catalog.find_capability("issue create", ready_only=True)
    assert all(r.auth_status != "required" for r in results)


def test_configured_token_promotes_to_configured(tmp_path: Path) -> None:
    """An auth file existing under the per-test cache root should flip
    auth_status from 'required' to 'configured' for that API."""
    from cocoon import auth as auth_module
    auth_module.write_token_env("linear", {"TOKEN": "lin_test"})
    results = catalog.find_capability("create a linear issue")
    linear = next(r for r in results if r.api == "linear")
    assert linear.auth_status == "configured"


def test_list_apis_includes_auth_status_and_sorts_ready_first() -> None:
    summaries = catalog.list_apis()
    statuses = [s.auth_status for s in summaries]
    # The first ready entry must precede the first required entry.
    if "required" in statuses and any(s in ("none", "configured") for s in statuses):
        first_required = statuses.index("required")
        first_ready = min(i for i, s in enumerate(statuses)
                          if s in ("none", "configured"))
        assert first_ready < first_required


def test_list_apis_ready_only_filters() -> None:
    summaries = catalog.list_apis(ready_only=True)
    assert summaries  # bundled corpus has hackernews (auth_type=none)
    assert all(s.auth_status != "required" for s in summaries)


def test_describe_includes_auth_status() -> None:
    cap = catalog.describe_capability("hackernews", "stories.top")
    assert cap.auth_status == "none"


def test_refresh_catalog_rewrites_cache(tmp_path: Path) -> None:
    cache_file = tmp_path / "catalog" / catalog.CACHE_FILE
    catalog.load_catalog()
    first_mtime = cache_file.stat().st_mtime_ns
    # Simulate stale cache by stomping it.
    cache_file.write_text("[]")
    catalog.refresh_catalog()
    assert cache_file.read_text() != "[]"
    assert cache_file.stat().st_mtime_ns >= first_mtime
