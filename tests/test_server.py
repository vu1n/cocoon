from cocoon.server import _cap, _format_result, _try_json


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
