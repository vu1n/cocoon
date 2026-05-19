"""Unit tests for the per-CLI agent-context extraction.

Uses a hand-trimmed fixture of the real hackernews-pp-cli agent-context
output (captured at 1.0.0). If upstream evolves the schema, these tests
will catch it loudly rather than silently degrading discovery.
"""

import json
from pathlib import Path

import pytest

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
