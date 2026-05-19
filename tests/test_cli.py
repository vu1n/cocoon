"""Black-box tests for the CLI: invoke `main()` with argv lists and assert
on stdout / filesystem state. Subprocess calls (the `claude mcp add` shell-out
in `init`) are monkeypatched, not actually spawned."""

import json
import subprocess
from pathlib import Path

from cocoon.cli import main


def test_init_print_emits_shell_command_and_snippet(capsys) -> None:
    assert main(["init", "--print"]) == 0
    out = capsys.readouterr().out
    assert "claude mcp add cocoon --scope user -- uvx cocoon serve" in out
    parsed = json.loads(out[out.index("{"):])
    assert parsed["mcpServers"]["cocoon"] == {"command": "uvx", "args": ["cocoon", "serve"]}


def test_init_default_registers_via_claude_mcp_add(monkeypatch, capsys) -> None:
    """No flags → registers with claude-code by shelling to `claude mcp add`."""
    calls: list[list[str]] = []

    def fake_which(name: str) -> str | None:
        return "/fake/claude" if name == "claude" else None

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("cocoon.cli.shutil.which", fake_which)
    monkeypatch.setattr("cocoon.cli.subprocess.run", fake_run)

    assert main(["init"]) == 0
    # First call removes the existing entry; second call adds.
    assert calls[0][:5] == ["/fake/claude", "mcp", "remove", "cocoon", "--scope"]
    assert calls[1][:5] == ["/fake/claude", "mcp", "add", "cocoon", "--scope"]
    # Command + args are appended after `--`.
    sep = calls[1].index("--")
    assert calls[1][sep + 1 :] == ["uvx", "cocoon", "serve"]


def test_init_propagates_custom_command_to_claude_mcp_add(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("cocoon.cli.shutil.which", lambda name: "/fake/claude")
    monkeypatch.setattr("cocoon.cli.subprocess.run", fake_run)

    assert main(["init", "--command", "uv run --directory /repo cocoon serve"]) == 0
    sep = calls[1].index("--")
    assert calls[1][sep + 1 :] == ["uv", "run", "--directory", "/repo", "cocoon", "serve"]


def test_init_fails_clearly_without_claude_cli(monkeypatch, capsys) -> None:
    monkeypatch.setattr("cocoon.cli.shutil.which", lambda name: None)
    assert main(["init"]) == 1
    err = capsys.readouterr().err
    assert "claude" in err and "PATH" in err


def test_init_surfaces_claude_mcp_add_failure(monkeypatch, capsys) -> None:
    def fake_run(cmd, **kwargs):
        if cmd[2] == "remove":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 3, stdout="", stderr="upstream said no")

    monkeypatch.setattr("cocoon.cli.shutil.which", lambda name: "/fake/claude")
    monkeypatch.setattr("cocoon.cli.subprocess.run", fake_run)

    assert main(["init"]) == 3
    assert "upstream said no" in capsys.readouterr().err


def test_init_print_command_quoted_args_preserved(capsys) -> None:
    """shlex.split + shlex.join keep `--tag 'group a'` as one shell token."""
    assert main(["init", "--print", "--command", "cocoon serve --tag 'group a'"]) == 0
    out = capsys.readouterr().out
    assert "claude mcp add cocoon --scope user -- cocoon serve --tag 'group a'" in out


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
    # `stories.top` is a real `pp:endpoint` capability from the bundled
    # hackernews agent-context.
    assert main(["describe", "hackernews", "stories.top", "--json"]) == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["api"] == "hackernews"
    assert parsed["tool"] == "stories.top"


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
