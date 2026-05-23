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
    assert "claude mcp add cocoon --scope user -- uvx --from cocoon-mcp cocoon serve" in out
    parsed = json.loads(out[out.index("{"):])
    assert parsed["mcpServers"]["cocoon"] == {
        "command": "uvx", "args": ["--from", "cocoon-mcp", "cocoon", "serve"],
    }


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
    assert calls[1][sep + 1 :] == ["uvx", "--from", "cocoon-mcp", "cocoon", "serve"]


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
    # bundled corpus has multiple payments APIs (stripe, mercury, ...);
    # the filter should at least surface stripe.
    assert "stripe" in {s["api"] for s in parsed}


def test_call_rejects_malformed_arg() -> None:
    assert main(["call", "hackernews", "doctor", "--arg", "noequals"]) == 2


def test_call_rejects_invalid_json_args() -> None:
    assert main(["call", "hackernews", "doctor", "--json-args", "{not json"]) == 2


def test_call_rejects_non_object_json_args() -> None:
    assert main(["call", "hackernews", "doctor", "--json-args", "[1,2,3]"]) == 2


# ---------------------------------------------------------------------------
# Agent-mode (RC6): when invoked via terminal fallback, agents need
# structured JSON on stdout/stderr instead of human-formatted text.
# ---------------------------------------------------------------------------

def test_agent_mode_describe_returns_json(monkeypatch, capsys) -> None:
    """COCOON_AGENT_MODE=1 flips describe to JSON even without --json."""
    monkeypatch.setenv("COCOON_AGENT_MODE", "1")
    assert main(["describe", "hackernews", "stories.top"]) == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["api"] == "hackernews"
    assert parsed["tool"] == "stories.top"


def test_agent_mode_argparse_error_structured(monkeypatch, capsys) -> None:
    """`describe <api>` (missing the tool arg) is the canonical RC6 case
    the postmortem cited: the model passed one positional, got argparse's
    human-formatted 'the following arguments are required' text wrapped
    in hermes's terminal envelope. In agent mode, it gets a stable
    {error: invalid_arguments, message, detail} instead."""
    monkeypatch.setenv("COCOON_AGENT_MODE", "1")
    rc = main(["describe", "hackernews"])
    assert rc == 2
    parsed = json.loads(capsys.readouterr().err)
    assert parsed["error"] == "invalid_arguments"
    assert "required" in parsed["message"] or "tool" in parsed["message"]


def test_agent_mode_unknown_subcommand_structured(monkeypatch, capsys) -> None:
    monkeypatch.setenv("COCOON_AGENT_MODE", "1")
    rc = main(["bogus-subcommand"])
    assert rc == 2
    parsed = json.loads(capsys.readouterr().err)
    assert parsed["error"] == "invalid_arguments"


def test_agent_mode_no_subcommand_structured(monkeypatch, capsys) -> None:
    """Without agent mode, `cocoon` alone prints help. With agent mode it
    needs to return a JSON error so the calling agent can react."""
    monkeypatch.setenv("COCOON_AGENT_MODE", "1")
    rc = main([])
    assert rc == 2
    parsed = json.loads(capsys.readouterr().err)
    assert parsed["error"] == "invalid_arguments"


def test_find_human_includes_auth_tag(capsys) -> None:
    """The human-readable find output prefixes each row with a readiness
    tag so the user can see at a glance which results are callable now."""
    assert main(["find", "hacker news"]) == 0
    out = capsys.readouterr().out
    # hackernews is auth_type=none → ready marker.
    assert any(line.startswith("* hackernews/") for line in out.splitlines())


def test_find_ready_only_filters_human(capsys) -> None:
    assert main(["find", "issue create", "--ready-only", "--json"]) == 0
    parsed = json.loads(capsys.readouterr().out)
    assert all(r["auth_status"] != "required" for r in parsed)


def test_list_ready_only_filters(capsys) -> None:
    assert main(["list", "--ready-only", "--json"]) == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed and all(s["auth_status"] != "required" for s in parsed)


def test_ready_subcommand_json_groups_by_status(capsys) -> None:
    assert main(["ready", "--json"]) == 0
    parsed = json.loads(capsys.readouterr().out)
    assert "no_auth" in parsed and "configured" in parsed
    # Bundled dev catalog has hackernews under no_auth.
    assert any(s["api"] == "hackernews" for s in parsed["no_auth"])


def test_ready_subcommand_human(capsys) -> None:
    assert main(["ready"]) == 0
    out = capsys.readouterr().out
    assert "No auth required" in out
    assert "hackernews" in out


def test_ready_after_auth_setup_lists_configured(tmp_path: Path, capsys) -> None:
    """Writing an auth file should make the API show up under
    `configured` in the `ready` output."""
    assert main(["auth", "linear", "--token", "lin_test"]) == 0
    capsys.readouterr()  # flush auth's stdout
    assert main(["ready", "--json"]) == 0
    parsed = json.loads(capsys.readouterr().out)
    assert any(s["api"] == "linear" for s in parsed["configured"])


def test_auth_dispatches_token_paste_for_api_key(tmp_path: Path, monkeypatch) -> None:
    """No flags + TTY + api_key auth_type → token_paste_flow.
    Pasted value lands in <API>_TOKEN."""
    from cocoon import catalog as catalog_module
    monkeypatch.setattr(catalog_module, "auth_type", lambda api: "api_key")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "tok_xyz")
    assert main(["auth", "linear"]) == 0
    written = json.loads((tmp_path / "auth" / "linear.json").read_text())
    assert written == {"env": {"LINEAR_TOKEN": "tok_xyz"}}


def test_auth_dispatches_cookie_flow_for_cookie_api(tmp_path: Path, monkeypatch) -> None:
    """Cookie auth_type → cookie_flow (stubbed here to avoid touching
    real Chrome or browser_cookie3)."""
    from cocoon import auth_flows, catalog as catalog_module
    monkeypatch.setattr(catalog_module, "auth_type", lambda api: "cookie")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr(auth_flows, "cookie_flow",
                        lambda api: {f"{api.upper()}_COOKIE": "stubbed=value"})
    assert main(["auth", "airbnb"]) == 0
    written = json.loads((tmp_path / "auth" / "airbnb.json").read_text())
    assert written == {"env": {"AIRBNB_COOKIE": "stubbed=value"}}


def test_auth_dispatches_token_paste_empty_input_fails(tmp_path: Path, monkeypatch) -> None:
    """An empty paste returns None from the flow; no file is written."""
    from cocoon import catalog as catalog_module
    monkeypatch.setattr(catalog_module, "auth_type", lambda api: "api_key")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    assert main(["auth", "linear"]) == 1
    assert not (tmp_path / "auth" / "linear.json").exists()


def test_auth_no_flags_non_tty_errors(capsys, monkeypatch) -> None:
    """Non-TTY (agent-mode bash-fallback) never dispatches to a flow
    — agent must pass --token / --env explicitly."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert main(["auth", "airbnb"]) == 2


def test_auth_rejects_setup_for_auth_none_api(capsys, monkeypatch) -> None:
    """`cocoon auth hackernews` is nonsensical (no auth needed); the
    flow returns None and main() exits non-zero."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    # hackernews is auth_type=none in the bundled dev catalog.
    assert main(["auth", "hackernews"]) == 1
    err = capsys.readouterr().err
    assert "doesn't require auth" in err


def test_non_agent_mode_argparse_error_human(capsys) -> None:
    """Non-agent mode keeps argparse-style human-readable error."""
    rc = main(["describe", "hackernews"])
    assert rc == 2
    err = capsys.readouterr().err
    # Plain text, not JSON
    assert "required" in err.lower() or "tool" in err.lower()
    assert not err.lstrip().startswith("{")
