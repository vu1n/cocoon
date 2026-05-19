from cocoon.server import _cap, _format_result, _try_json, cocoon as cocoon_tool


def test_try_json_parses_object() -> None:
    assert _try_json('{"a": 1}') == {"a": 1}


def test_try_json_parses_array() -> None:
    assert _try_json("[1, 2, 3]") == [1, 2, 3]


def test_try_json_ignores_plain_text() -> None:
    assert _try_json("hello world") is None


def test_try_json_ignores_invalid_json() -> None:
    assert _try_json("{not json") is None


def test_format_result_emits_json_key_when_decodable() -> None:
    out = _format_result(0, '{"ok": true}', "")
    assert out == {"exit_code": 0, "json": {"ok": True}}


def test_format_result_emits_stdout_when_plain_text() -> None:
    out = _format_result(0, "plain output\n", "")
    assert out == {"exit_code": 0, "stdout": "plain output\n"}


def test_format_result_includes_stderr_when_present() -> None:
    out = _format_result(1, "", "boom")
    assert out["stderr"] == "boom"
    assert out["exit_code"] == 1


def test_cap_short_text_passes_through() -> None:
    assert _cap("hello") == "hello"


def test_cap_truncates_with_marker() -> None:
    long = "x" * (64 * 1024 + 100)
    capped = _cap(long)
    assert "truncated" in capped
    assert capped.startswith("x" * 64)


# ---------------------------------------------------------------------------
# Dispatcher tests for the single `cocoon` MCP tool. We call the decorated
# function directly: the @_catch_cocoon_errors wrapper stays in place (that
# being the boundary contract) but we skip the MCP transport layer.
# ---------------------------------------------------------------------------

async def _call(**kwargs):
    return await cocoon_tool(**kwargs)


async def test_action_find_returns_results() -> None:
    out = await _call(action="find", query="hacker news")
    assert isinstance(out, list)
    assert any(r["api"] == "hackernews" for r in out)


async def test_action_find_without_query_returns_error() -> None:
    out = await _call(action="find")
    assert out["error"] == "cocoon_error"
    assert "query" in out["message"]


async def test_action_describe_returns_capability() -> None:
    # `stories.top` is a real `pp:endpoint`-annotated capability from the
    # bundled hackernews agent-context. `doctor` was hand-curated in the
    # old dev catalog but isn't actually marked as an endpoint upstream
    # (it's a CLI health check, not an API operation).
    out = await _call(action="describe", api="hackernews", tool="stories.top")
    assert out["api"] == "hackernews"
    assert out["tool"] == "stories.top"


async def test_action_describe_unknown_returns_capability_not_found() -> None:
    out = await _call(action="describe", api="hackernews", tool="nope")
    assert out["error"] == "capability_not_found"


async def test_action_describe_missing_args_returns_error() -> None:
    out = await _call(action="describe", api="hackernews")
    assert out["error"] == "cocoon_error"
    assert out["detail"]["missing"] == ["tool"]


async def test_action_describe_missing_both_lists_both() -> None:
    out = await _call(action="describe")
    assert out["detail"]["missing"] == ["api", "tool"]


async def test_action_list_returns_summaries() -> None:
    out = await _call(action="list")
    assert isinstance(out, list)
    assert all("api" in s and "endpoint_count" in s for s in out)


async def test_action_list_filter_applied() -> None:
    out = await _call(action="list", filter="payments")
    # bundled corpus has multiple payments APIs; filter must at least include stripe.
    assert "stripe" in {s["api"] for s in out}


async def test_unknown_action_returns_error() -> None:
    out = await _call(action="bogus")  # type: ignore[arg-type]
    assert out["error"] == "cocoon_error"
    assert "unknown action" in out["message"]


async def test_action_call_missing_api_returns_error() -> None:
    out = await _call(action="call", tool="doctor")
    assert out["error"] == "cocoon_error"
    assert out["detail"]["missing"] == ["api"]


def test_invocation_for_returns_positionals_and_argv_path() -> None:
    """The bundled hackernews aggregate marks `itemId` as a positional for
    `items.get`, and the real cobra command is `items <itemId>` (the `.get`
    verb suffix is a logical name, not a real subcommand). _invocation_for
    must surface both pieces so do_call passes the right argv shape."""
    from cocoon.server import _invocation_for
    positionals, argv_path = _invocation_for("hackernews", "items.get")
    assert positionals == ("itemId",)
    assert argv_path == ("items",)


def test_invocation_for_nested_subcommand_keeps_full_path() -> None:
    """`stories.top` IS a real nested cobra subcommand. argv_path keeps both
    segments; positionals is empty."""
    from cocoon.server import _invocation_for
    positionals, argv_path = _invocation_for("hackernews", "stories.top")
    assert positionals == ()
    assert argv_path == ("stories", "top")


def test_invocation_for_returns_empty_for_unknown_tool() -> None:
    """`doctor` is callable on hackernews-pp-cli but isn't `pp:endpoint`-
    annotated upstream; the catalog raises CapabilityNotFound. We swallow
    that and fall back to both (), which keeps tool_argv on the flags-only
    + dot-split path."""
    from cocoon.server import _invocation_for
    assert _invocation_for("hackernews", "doctor") == ((), ())
