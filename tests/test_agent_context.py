"""Unit tests for the per-CLI agent-context extraction.

Uses a hand-trimmed fixture of the real hackernews-pp-cli agent-context
output (captured at 1.0.0). If upstream evolves the schema, these tests
will catch it loudly rather than silently degrading discovery.
"""

import json
from pathlib import Path

from cocoon import agent_context

HN_FIXTURE: dict = {
    "schema_version": "2",
    "cli": {"name": "hackernews-pp-cli", "description": "Hacker News...", "version": "1.0.0"},
    "auth": {"mode": "none", "env_vars": []},
    "commands": [
        {
            "name": "items", "use": "items <itemId>",
            "short": "Get details for a specific story, comment, job, or poll",
            "annotations": {"pp:endpoint": "items.get", "mcp:read-only": "true"},
            "subcommands": [
                {"name": "thread", "use": "thread <id>",
                 "short": "Print a thread's comment tree",
                 "flags": [{"name": "depth", "type": "int", "usage": "...", "default": "0"}]},
            ],
        },
        {
            "name": "stories", "use": "stories", "short": "Browse top, new, and best HN stories",
            "subcommands": [
                {"name": "top", "use": "top",
                 "short": "Get the current top stories on Hacker News",
                 "annotations": {"pp:endpoint": "stories.top"},
                 "flags": [{"name": "limit", "type": "int", "usage": "Max results", "default": "30"}]},
                {"name": "best", "use": "best",
                 "short": "Get the highest-voted stories",
                 "annotations": {"pp:endpoint": "stories.best"},
                 "flags": [{"name": "limit", "type": "int", "usage": "Max results", "default": "30"}]},
            ],
        },
        {
            "name": "doctor", "use": "doctor", "short": "Check CLI health",
            # No pp:endpoint annotation → not surfaced as a capability.
        },
    ],
}


def test_to_capabilities_emits_only_annotated_commands() -> None:
    caps = agent_context.to_capabilities("hackernews", HN_FIXTURE)
    tools = sorted(c["tool"] for c in caps)
    assert tools == ["items.get", "stories.best", "stories.top"]


def test_to_capabilities_picks_up_flags() -> None:
    caps = agent_context.to_capabilities("hackernews", HN_FIXTURE)
    top = next(c for c in caps if c["tool"] == "stories.top")
    assert top["params_schema"] == {"limit": "int?"}


def test_to_capabilities_picks_up_positional_required_args() -> None:
    caps = agent_context.to_capabilities("hackernews", HN_FIXTURE)
    items = next(c for c in caps if c["tool"] == "items.get")
    # `items <itemId>` → required positional `itemId`
    assert items["params_schema"]["itemId"] == "string"


def test_to_capabilities_summary_from_short() -> None:
    caps = agent_context.to_capabilities("hackernews", HN_FIXTURE)
    top = next(c for c in caps if c["tool"] == "stories.top")
    assert "top stories" in top["summary"].lower()


def test_to_capabilities_walks_subcommand_tree() -> None:
    """`stories.top` is two levels deep; the walker must recurse to find it."""
    caps = agent_context.to_capabilities("hackernews", HN_FIXTURE)
    assert any(c["tool"] == "stories.top" for c in caps)


def test_to_capabilities_carries_argv_path_for_nested_subcommand() -> None:
    """`stories.top` is annotated at the leaf `top` under `stories`; the
    cobra invocation path is `("stories", "top")`."""
    caps = agent_context.to_capabilities("hackernews", HN_FIXTURE)
    top = next(c for c in caps if c["tool"] == "stories.top")
    assert top["argv_path"] == ("stories", "top")


def test_to_capabilities_carries_argv_path_for_bare_root_with_verb_annotation() -> None:
    """`items` is annotated at the ROOT level with `pp:endpoint=items.get`
    (the `.get` is a verb suffix, not a real subcommand). The cobra
    invocation path is just `("items",)` — splitting the dotted pp:endpoint
    would incorrectly produce `("items", "get")`."""
    caps = agent_context.to_capabilities("hackernews", HN_FIXTURE)
    items = next(c for c in caps if c["tool"] == "items.get")
    assert items["argv_path"] == ("items",)


def test_to_capabilities_empty_on_no_commands() -> None:
    assert agent_context.to_capabilities("x", {"commands": []}) == []
    assert agent_context.to_capabilities("x", {}) == []


# Some CLIs (e.g. digg) emit `commands`/`subcommands`/`flags` as name-keyed
# maps rather than lists. _as_objects normalizes both shapes; one bad shape
# must never crash to_capabilities, since catalog._merged_view walks every
# cached context and a single raise there breaks discovery catalog-wide.
DIGG_FIXTURE: dict = {
    "schema_version": "2",
    "commands": {
        "search": {
            "name": "search", "use": "search <query>",
            "short": "Search clusters",
            "annotations": {"pp:endpoint": "search.run"},
            "flags": {"--since": {"name": "since", "type": "string", "usage": "window"}},
        },
        "author": {
            "name": "author", "use": "author [username]", "short": "no endpoint here",
            "subcommands": {
                "clusters": {"name": "clusters", "use": "clusters",
                             "short": "List an author's clusters",
                             "annotations": {"pp:endpoint": "author.clusters"}},
            },
        },
    },
}


def test_to_capabilities_handles_dict_shaped_commands() -> None:
    caps = agent_context.to_capabilities("digg", DIGG_FIXTURE)
    tools = {c["tool"] for c in caps}
    assert tools == {"search.run", "author.clusters"}


def test_to_capabilities_handles_dict_shaped_flags() -> None:
    caps = agent_context.to_capabilities("digg", DIGG_FIXTURE)
    search = next(c for c in caps if c["tool"] == "search.run")
    # positional from `use` + flag from the name-keyed flags map
    assert search["params_schema"] == {"query": "string", "since": "string?"}


def test_to_capabilities_walks_dict_shaped_subcommands() -> None:
    caps = agent_context.to_capabilities("digg", DIGG_FIXTURE)
    clusters = next(c for c in caps if c["tool"] == "author.clusters")
    assert clusters["argv_path"] == ("author", "clusters")


def test_to_capabilities_skips_non_dict_elements() -> None:
    # A stray string in a commands list (or any non-object) is dropped, not raised.
    ctx = {"commands": ["bogus", {"name": "ok", "use": "ok",
                                  "annotations": {"pp:endpoint": "ok.run"}}]}
    caps = agent_context.to_capabilities("x", ctx)
    assert [c["tool"] for c in caps] == ["ok.run"]


def test_to_capabilities_tolerates_scalar_commands() -> None:
    # commands as a string/None shouldn't blow up — returns empty.
    assert agent_context.to_capabilities("x", {"commands": "nonsense"}) == []
    assert agent_context.to_capabilities("x", {"commands": None}) == []


# digg's real agent-context redundantly lists each command BOTH nested
# (feed → subcommand raw) AND flattened as a sibling top-level key ("feed raw"
# whose value has name="raw"). Walking both yields two caps for feed.raw with
# different argv_paths; we must keep the correct (nested) one, not duplicate.
DIGG_FLATTENED_FIXTURE: dict = {
    "commands": {
        "feed": {
            "name": "feed", "use": "feed", "short": "Feed commands",
            "subcommands": {
                "raw": {"name": "raw", "use": "raw",
                        "short": "Raw feed",
                        "annotations": {"pp:endpoint": "feed.raw"}},
            },
        },
        "feed raw": {  # flattened duplicate — value carries the short name only
            "name": "raw", "use": "raw", "short": "Raw feed",
            "annotations": {"pp:endpoint": "feed.raw"},
        },
    },
}


def test_to_capabilities_dedupes_flattened_duplicate_keeping_correct_path() -> None:
    caps = agent_context.to_capabilities("digg", DIGG_FLATTENED_FIXTURE)
    assert len(caps) == 1, "flattened + nested duplicate must collapse to one cap"
    assert caps[0]["tool"] == "feed.raw"
    # the nested path is the real cobra invocation; the flattened ("raw",) is wrong
    assert caps[0]["argv_path"] == ("feed", "raw")


def test_auth_mode_reads_field() -> None:
    assert agent_context.auth_mode(HN_FIXTURE) == "none"
    assert agent_context.auth_mode({"auth": {"mode": "api_key"}}) == "api_key"


def test_auth_mode_returns_none_for_missing() -> None:
    assert agent_context.auth_mode(None) is None
    assert agent_context.auth_mode({}) is None
    assert agent_context.auth_mode({"auth": "not a dict"}) is None


def test_cached_returns_none_when_missing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("COCOON_CACHE_DIR", str(tmp_path))
    assert agent_context.cached("never-installed") is None


def test_cached_round_trip(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("COCOON_CACHE_DIR", str(tmp_path))
    (tmp_path / "agent-context").mkdir()
    (tmp_path / "agent-context" / "x.json").write_text(json.dumps(HN_FIXTURE))
    loaded = agent_context.cached("x")
    assert loaded == HN_FIXTURE


def test_cached_tolerates_corrupt_json(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("COCOON_CACHE_DIR", str(tmp_path))
    (tmp_path / "agent-context").mkdir()
    (tmp_path / "agent-context" / "x.json").write_text("{not json")
    assert agent_context.cached("x") is None


# ---------------------------------------------------------------------------
# Positional-arg parsing helper
# ---------------------------------------------------------------------------


class TestPositionalTokens:
    def test_required_only(self) -> None:
        assert agent_context._positional_tokens("items <itemId>") == [("itemId", True)]

    def test_optional_only(self) -> None:
        assert agent_context._positional_tokens("stats [user]") == [("user", False)]

    def test_required_then_optional(self) -> None:
        assert agent_context._positional_tokens("save <name> [--flag]") == [
            ("name", True), ("--flag", False),
        ]

    def test_no_positionals(self) -> None:
        assert agent_context._positional_tokens("stories") == []

    def test_handles_unclosed(self) -> None:
        assert agent_context._positional_tokens("items <unclosed") == []
