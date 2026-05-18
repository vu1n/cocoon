"""Black-box tests for the CLI: invoke `main()` with argv lists and assert
on stdout / filesystem state. Avoids spinning up subprocesses."""

import json
from pathlib import Path

import pytest

from cocoon.cli import main


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("COCOON_CACHE_DIR", str(tmp_path))


def test_init_print_emits_snippet(capsys) -> None:
    assert main(["init", "--print"]) == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert "cocoon" in parsed["mcpServers"]
    assert parsed["mcpServers"]["cocoon"]["args"] == ["cocoon", "serve"]


def test_init_writes_into_named_host(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    assert main(["init", "--host", "claude-code"]) == 0
    cfg = tmp_path / "home" / ".claude" / "mcp.json"
    data = json.loads(cfg.read_text())
    assert data["mcpServers"]["cocoon"] == {"command": "uvx", "args": ["cocoon", "serve"]}


def test_init_preserves_other_servers(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    cfg = tmp_path / "home" / ".claude" / "mcp.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"mcpServers": {"other": {"command": "echo"}}}))
    assert main(["init", "--host", "claude-code"]) == 0
    data = json.loads(cfg.read_text())
    assert set(data["mcpServers"]) == {"other", "cocoon"}


def test_init_requires_host_or_print(capsys) -> None:
    with pytest.raises(SystemExit):
        main(["init"])


def test_init_command_override_print(capsys) -> None:
    assert main(["init", "--print", "--command", "/usr/local/bin/cocoon serve"]) == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["mcpServers"]["cocoon"] == {
        "command": "/usr/local/bin/cocoon", "args": ["serve"],
    }


def test_init_command_override_writes_into_host(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    assert main([
        "init", "--host", "claude-code",
        "--command", "uv run --directory /repo cocoon serve",
    ]) == 0
    data = json.loads((tmp_path / "home" / ".claude" / "mcp.json").read_text())
    assert data["mcpServers"]["cocoon"] == {
        "command": "uv",
        "args": ["run", "--directory", "/repo", "cocoon", "serve"],
    }


def test_init_command_quoted_args_preserved(capsys) -> None:
    """shlex handles quoted strings so a flag with spaces stays one arg."""
    assert main(["init", "--print", "--command", "cocoon serve --tag 'group a'"]) == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["mcpServers"]["cocoon"]["args"] == ["serve", "--tag", "group a"]


def test_init_empty_command_rejected(capsys) -> None:
    assert main(["init", "--print", "--command", "   "]) == 2
    err = capsys.readouterr().err
    assert "non-empty" in err


def test_auth_with_token_writes_file(tmp_path: Path, capsys) -> None:
    assert main(["auth", "linear", "--token", "lin_abc"]) == 0
    written = tmp_path / "auth" / "linear.json"
    assert json.loads(written.read_text()) == {"env": {"TOKEN": "lin_abc"}}


def test_auth_with_explicit_env(tmp_path: Path) -> None:
    assert main(["auth", "stripe",
                 "--env", "STRIPE_KEY=sk_test_123",
                 "--env", "STRIPE_VERSION=2025-01-01"]) == 0
    data = json.loads((tmp_path / "auth" / "stripe.json").read_text())
    assert data["env"] == {"STRIPE_KEY": "sk_test_123", "STRIPE_VERSION": "2025-01-01"}


def test_auth_requires_credentials() -> None:
    assert main(["auth", "linear"]) == 2


def test_auth_rejects_malformed_env() -> None:
    assert main(["auth", "linear", "--env", "noequalssign"]) == 2


def test_catalog_refresh_succeeds(monkeypatch, capsys) -> None:
    monkeypatch.delenv("COCOON_CATALOG_URL", raising=False)
    assert main(["catalog", "refresh"]) == 0
    assert "apis" in capsys.readouterr().out


def test_doctor_runs_without_crashing(capsys) -> None:
    rc = main(["doctor"])
    # rc is 0 if both bwrap/sandbox-exec AND printing-press are present;
    # in CI / test environments it's usually 1. Either is fine for this
    # test; we just want a clean run.
    assert rc in (0, 1)
    out = capsys.readouterr().out
    assert "cocoon" in out
    assert "sandbox backend" in out


def test_no_subcommand_prints_help_and_returns_2(capsys) -> None:
    assert main([]) == 2
    out = capsys.readouterr().out
    assert "cocoon" in out


def test_find_json_output(capsys) -> None:
    assert main(["find", "hacker news", "--json"]) == 0
    parsed = json.loads(capsys.readouterr().out)
    assert isinstance(parsed, list)
    assert any(r["api"] == "hackernews" for r in parsed)


def test_find_human_output(capsys) -> None:
    assert main(["find", "linear issue"]) == 0
    out = capsys.readouterr().out
    assert "linear/" in out


def test_describe_json(capsys) -> None:
    assert main(["describe", "hackernews", "doctor", "--json"]) == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["api"] == "hackernews"
    assert parsed["tool"] == "doctor"


def test_list_human(capsys) -> None:
    assert main(["list"]) == 0
    out = capsys.readouterr().out
    assert "hackernews" in out


def test_list_filter_json(capsys) -> None:
    assert main(["list", "--filter", "payments", "--json"]) == 0
    parsed = json.loads(capsys.readouterr().out)
    assert {s["api"] for s in parsed} == {"stripe"}


def test_call_rejects_malformed_arg() -> None:
    assert main(["call", "hackernews", "doctor", "--arg", "noequals"]) == 2


def test_call_rejects_invalid_json_args() -> None:
    assert main(["call", "hackernews", "doctor", "--json-args", "{not json"]) == 2


def test_call_rejects_non_object_json_args() -> None:
    assert main(["call", "hackernews", "doctor", "--json-args", "[1,2,3]"]) == 2
