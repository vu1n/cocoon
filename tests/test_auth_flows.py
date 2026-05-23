"""Tests for the generic auth flows. cookie_flow uses
browser_cookie3 to read the user's Chrome jar; we stub that so the
tests don't depend on a live browser or keychain access."""

import sys

import pytest

from cocoon import auth_flows


def test_run_none_auth_type_errors(capsys) -> None:
    """Asking to set up auth for an API that doesn't need any is a
    user error — we don't silently write an empty auth file."""
    assert auth_flows.run("hackernews", "none") is None
    assert "doesn't require auth" in capsys.readouterr().err


def test_run_dispatches_cookie_flow(monkeypatch) -> None:
    called = {}
    def fake_cookie_flow(api):
        called["api"] = api
        return {"X": "y"}
    monkeypatch.setattr(auth_flows, "cookie_flow", fake_cookie_flow)
    out = auth_flows.run("airbnb", "cookie")
    assert called == {"api": "airbnb"}
    assert out == {"X": "y"}


def test_run_dispatches_token_paste_for_everything_else(monkeypatch) -> None:
    """api_key / bearer_token / unfamiliar auth_types all fall through
    to the paste flow — generic by design."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "tok_123")
    assert auth_flows.run("linear", "api_key") == {"LINEAR_TOKEN": "tok_123"}
    assert auth_flows.run("roam", "bearer") == {"ROAM_TOKEN": "tok_123"}
    assert auth_flows.run("custom-thing", "weird_new_auth") == {"CUSTOM_THING_TOKEN": "tok_123"}


def test_token_paste_flow_empty_input_returns_none(monkeypatch) -> None:
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    assert auth_flows.token_paste_flow("api", "api_key") is None


def test_token_paste_flow_eof_returns_none(monkeypatch) -> None:
    """Ctrl-D on the prompt shouldn't crash — surface as None."""
    def raise_eof(prompt=""):
        raise EOFError
    monkeypatch.setattr("builtins.input", raise_eof)
    assert auth_flows.token_paste_flow("api", "api_key") is None


def test_env_var_naming_uppercases_and_substitutes_hyphens() -> None:
    assert auth_flows._env_var_for("airbnb", suffix="COOKIE") == "AIRBNB_COOKIE"
    assert auth_flows._env_var_for("alaska-airlines", suffix="TOKEN") == "ALASKA_AIRLINES_TOKEN"


def test_homepage_default_uses_convention() -> None:
    assert auth_flows._homepage_for("airbnb") == "https://www.airbnb.com"
    assert auth_flows._homepage_for("ebay") == "https://www.ebay.com"


def test_homepage_override_wins(monkeypatch) -> None:
    """Override map applies for cases where the convention fails."""
    monkeypatch.setitem(auth_flows._HOMEPAGE_OVERRIDES, "weird-api",
                        "https://app.weird.io/login")
    assert auth_flows._homepage_for("weird-api") == "https://app.weird.io/login"


def test_cookie_flow_missing_browser_cookie3_returns_none(monkeypatch, capsys) -> None:
    """If browser_cookie3 isn't installed cocoon should fail cleanly,
    not crash on ImportError. (We can't easily unimport here, so
    simulate by patching the module-level import to raise.)"""
    import builtins
    real_import = builtins.__import__
    def fake_import(name, *args, **kwargs):
        if name == "browser_cookie3":
            raise ImportError("simulated missing dep")
        return real_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert auth_flows.cookie_flow("airbnb") is None
    assert "browser_cookie3 not installed" in capsys.readouterr().err


def test_cookie_flow_no_cookies_returns_none(monkeypatch, capsys) -> None:
    """If the user opened the URL but isn't signed in, the jar is
    empty for that domain — surface as None, not as an empty auth
    file that would silently mask the next call's auth_missing."""
    class FakeCookieJar:
        def __iter__(self):
            return iter([])
    class FakeBC3:
        @staticmethod
        def chrome(domain_name):
            return FakeCookieJar()
    sys.modules["browser_cookie3"] = FakeBC3
    monkeypatch.setattr("webbrowser.open", lambda url: None)
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    try:
        assert auth_flows.cookie_flow("airbnb") is None
        assert "no cookies found" in capsys.readouterr().err
    finally:
        del sys.modules["browser_cookie3"]


def test_cookie_flow_serializes_header(monkeypatch) -> None:
    """Multiple cookies become a single `name1=v1; name2=v2` header
    under the conventional <API>_COOKIE env var."""
    class FakeCookie:
        def __init__(self, name, value):
            self.name, self.value = name, value
    class FakeBC3:
        @staticmethod
        def chrome(domain_name):
            return [FakeCookie("_user_attributes", "abc"),
                    FakeCookie("_session", "xyz")]
    sys.modules["browser_cookie3"] = FakeBC3
    monkeypatch.setattr("webbrowser.open", lambda url: None)
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    try:
        result = auth_flows.cookie_flow("airbnb")
        assert result is not None
        assert result["AIRBNB_COOKIE"] == "_user_attributes=abc; _session=xyz"
    finally:
        del sys.modules["browser_cookie3"]
