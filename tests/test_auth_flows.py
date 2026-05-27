"""Tests for the auth_flows delegator. The cookie path execs the
per-CLI `<api>-pp-cli auth login --chrome`; we stub subprocess.run
+ materialize so tests don't touch real binaries or browsers."""

import os
from pathlib import Path
from unittest.mock import MagicMock

from cocoon import auth_flows


def test_run_none_auth_type_errors(capsys) -> None:
    """Asking to set up auth for an API that doesn't need any is a
    user error — we don't silently write an empty auth file."""
    assert auth_flows.run("hackernews", "none") is None
    assert "doesn't require auth" in capsys.readouterr().err


def test_is_delegated_covers_browser_auth_types() -> None:
    """The cookie/session/composed family all defer to the CLI's own
    auth login subcommand. Token-class types do not."""
    assert auth_flows.is_delegated("cookie")
    assert auth_flows.is_delegated("session_handshake")
    assert auth_flows.is_delegated("composed")
    assert not auth_flows.is_delegated("api_key")
    assert not auth_flows.is_delegated("bearer_token")
    assert not auth_flows.is_delegated("none")


def test_run_dispatches_cookie_to_delegate(monkeypatch) -> None:
    called = {}
    def fake_delegate(api):
        called["api"] = api
        return {"_DELEGATED_TO": "press-auth", "_NOTE": "x"}
    monkeypatch.setattr(auth_flows, "delegate_login", fake_delegate)
    out = auth_flows.run("airbnb", "cookie")
    assert called == {"api": "airbnb"}
    assert out["_DELEGATED_TO"] == "press-auth"


def test_run_dispatches_token_paste_for_everything_else(monkeypatch) -> None:
    """api_key / bearer_token / unfamiliar auth_types all fall through
    to the paste flow — cocoon owns the credential for these."""
    monkeypatch.setattr("builtins.input", lambda prompt="": "tok_123")
    assert auth_flows.run("linear", "api_key") == {"LINEAR_TOKEN": "tok_123"}
    assert auth_flows.run("roam", "bearer") == {"ROAM_TOKEN": "tok_123"}
    assert auth_flows.run("custom-thing", "weird_new") == {"CUSTOM_THING_TOKEN": "tok_123"}


def test_token_paste_flow_empty_input_returns_none(monkeypatch) -> None:
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    assert auth_flows.token_paste_flow("api", "api_key") is None


def test_token_paste_flow_eof_returns_none(monkeypatch) -> None:
    def raise_eof(prompt=""):
        raise EOFError
    monkeypatch.setattr("builtins.input", raise_eof)
    assert auth_flows.token_paste_flow("api", "api_key") is None


def test_env_var_naming_uppercases_and_substitutes_hyphens() -> None:
    assert auth_flows._env_var_for("airbnb", suffix="COOKIE") == "AIRBNB_COOKIE"
    assert auth_flows._env_var_for("alaska-airlines", suffix="TOKEN") == "ALASKA_AIRLINES_TOKEN"


def test_delegate_login_execs_upstream_auth_login(monkeypatch, tmp_path) -> None:
    """delegate_login materializes the CLI binary, then execs
    `<binary> auth login --chrome`. Exit 0 produces the marker dict;
    nonzero returns None."""
    monkeypatch.setattr(auth_flows, "press_auth_dir", lambda: tmp_path)
    fake_binary = Path("/tmp/fake/airbnb-pp-cli")
    monkeypatch.setattr(auth_flows.materialize, "materialize", lambda api: fake_binary)
    calls = []
    def fake_run(argv):
        calls.append(argv)
        return MagicMock(returncode=0)
    monkeypatch.setattr(auth_flows.subprocess, "run", fake_run)
    result = auth_flows.delegate_login("airbnb")
    assert calls == [[str(fake_binary), "auth", "login", "--chrome"]]
    assert result is not None
    assert "_DELEGATED_TO" in result
    assert result["_DELEGATED_TO"] == "press-auth"


def _login_writing(monkeypatch, tmp_path, written: dict[str, str]):
    """Wire delegate_login so its `auth login` subprocess creates `written`
    (filename -> contents) under a tmp press-auth dir, and return the marker."""
    monkeypatch.setattr(auth_flows, "press_auth_dir", lambda: tmp_path)
    monkeypatch.setattr(auth_flows.materialize, "materialize",
                        lambda api: Path("/tmp/fake/x-pp-cli"))

    def fake_run(argv):
        for name, body in written.items():
            (tmp_path / name).write_text(body)
        return MagicMock(returncode=0)

    monkeypatch.setattr(auth_flows.subprocess, "run", fake_run)
    return auth_flows.delegate_login("x")


def test_delegate_login_records_changed_press_auth_file(monkeypatch, tmp_path) -> None:
    import json
    marker = _login_writing(monkeypatch, tmp_path, {"airbnb.com.json": "encrypted"})
    assert json.loads(marker["_PRESS_AUTH_FILES"]) == ["airbnb.com.json"]


def test_delegate_login_records_empty_when_login_writes_nothing(monkeypatch, tmp_path) -> None:
    import json
    # a pre-existing file that login does NOT touch must not be claimed
    (tmp_path / "someone-else.json").write_text("not mine")
    marker = _login_writing(monkeypatch, tmp_path, {})
    assert json.loads(marker["_PRESS_AUTH_FILES"]) == []


def test_press_auth_paths_maps_marker_to_files(monkeypatch, tmp_path) -> None:
    import json
    monkeypatch.setattr(auth_flows, "press_auth_dir", lambda: tmp_path)
    paths = auth_flows.press_auth_paths({"_PRESS_AUTH_FILES": json.dumps(["airbnb.com.json"])})
    assert paths == (tmp_path / "airbnb.com.json",)


def test_press_auth_paths_rejects_traversal(monkeypatch, tmp_path) -> None:
    import json
    monkeypatch.setattr(auth_flows, "press_auth_dir", lambda: tmp_path)
    paths = auth_flows.press_auth_paths(
        {"_PRESS_AUTH_FILES": json.dumps(["../../etc/passwd", "ok.json", "a/b.json"])})
    assert paths == (tmp_path / "ok.json",)  # traversal + separator entries dropped


def test_press_auth_paths_empty_when_marker_lacks_key() -> None:
    assert auth_flows.press_auth_paths({"_DELEGATED_TO": "press-auth"}) == ()
    assert auth_flows.press_auth_paths({"_PRESS_AUTH_FILES": "not-json"}) == ()


def test_press_auth_paths_rejects_sbpl_breaking_and_unsafe_names(monkeypatch, tmp_path) -> None:
    import json
    monkeypatch.setattr(auth_flows, "press_auth_dir", lambda: tmp_path)
    # a double-quote would break out of the SBPL (subpath "...") literal; a
    # space / control char is not a real press-auth name. Only the clean one survives.
    paths = auth_flows.press_auth_paths(
        {"_PRESS_AUTH_FILES": json.dumps(['evil".json', "spa ce.json", "x\ty.json", "ok.json"])})
    assert paths == (tmp_path / "ok.json",)


def test_delegate_login_detects_inplace_rewrite_with_unchanged_mtime(monkeypatch, tmp_path) -> None:
    """Content-hash snapshot catches a same-size, mtime-preserved rewrite that a
    (mtime, size) snapshot would miss — otherwise scoping silently falls back to
    the whole store."""
    import json
    monkeypatch.setattr(auth_flows, "press_auth_dir", lambda: tmp_path)
    monkeypatch.setattr(auth_flows.materialize, "materialize",
                        lambda api: Path("/tmp/fake/x-pp-cli"))
    cred = tmp_path / "a.com.json"
    cred.write_text("AAAA")
    st = cred.stat()

    def fake_run(argv):
        cred.write_text("BBBB")  # same length, different content
        os.utime(cred, ns=(st.st_atime_ns, st.st_mtime_ns))  # restore mtime
        return MagicMock(returncode=0)

    monkeypatch.setattr(auth_flows.subprocess, "run", fake_run)
    marker = auth_flows.delegate_login("x")
    assert json.loads(marker["_PRESS_AUTH_FILES"]) == ["a.com.json"]


def test_delegate_login_returns_none_on_nonzero_exit(monkeypatch, capsys) -> None:
    monkeypatch.setattr(auth_flows.materialize, "materialize",
                        lambda api: Path("/tmp/fake/x-pp-cli"))
    monkeypatch.setattr(auth_flows.subprocess, "run",
                        lambda argv: MagicMock(returncode=2))
    assert auth_flows.delegate_login("x") is None
    assert "exited 2" in capsys.readouterr().err


def test_delegate_login_returns_none_when_materialize_fails(monkeypatch, capsys) -> None:
    def boom(api):
        raise RuntimeError("network down")
    monkeypatch.setattr(auth_flows.materialize, "materialize", boom)
    assert auth_flows.delegate_login("x") is None
    assert "network down" in capsys.readouterr().err
