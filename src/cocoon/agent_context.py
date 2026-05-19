"""Per-CLI capability extraction via the `agent-context` subcommand.

Every printing-press CLI exposes an `agent-context` subcommand that emits
structured JSON describing its own command tree, auth requirements, and
per-command flags. This is the authoritative answer to "what does this
CLI expose?" — the registry.json only carries API-level metadata.

cocoon captures this JSON right after a successful `go install` and
writes it to ~/.cache/cocoon/agent-context/<api>.json. The catalog layer
then merges these caches over the bundled dev catalog so `find` /
`describe` see real endpoint schemas for any installed API.

Coherence rule: the local agent-context cache is authoritative for any
API whose binary is locally installed — it reflects what's actually
executable on this machine. Upstream/aggregated catalogs (when added)
serve only the pre-install discovery case.
"""

import json
import subprocess
from pathlib import Path
from typing import Any

from .errors import CocoonError
from .paths import agent_context_dir


def cache_path(api: str) -> Path:
    return agent_context_dir() / f"{api}.json"


def cached(api: str) -> dict | None:
    path = cache_path(api)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def capture(binary: Path, api: str) -> dict | None:
    """Run `<binary> agent-context`, parse, persist. Returns the parsed JSON
    on success, None on failure. Best-effort by design: the install itself
    already succeeded, and a missing agent-context just degrades discovery
    quality rather than breaking the call path."""
    try:
        result = subprocess.run(
            [str(binary), "agent-context"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None

    agent_context_dir().mkdir(parents=True, exist_ok=True)
    cache_path(api).write_text(json.dumps(data), encoding="utf-8")
    return data


def to_capabilities(api: str, ctx: dict) -> list[dict]:
    """Walk the agent-context command tree, emit a Capability dict for
    every command annotated with `pp:endpoint`. Returns the same shape
    catalog.find_capability et al. produce: {api, tool, summary, params_schema}.
    """
    out: list[dict] = []
    for cmd in ctx.get("commands", []):
        _visit(api, cmd, out)
    return out


def _visit(api: str, cmd: dict, out: list[dict]) -> None:
    annotations = cmd.get("annotations") or {}
    endpoint = annotations.get("pp:endpoint")
    if endpoint:
        out.append({
            "api": api,
            "tool": endpoint,
            "summary": cmd.get("short", ""),
            "params_schema": _params_schema(cmd),
        })
    for sub in cmd.get("subcommands", []) or []:
        _visit(api, sub, out)


def _params_schema(cmd: dict) -> dict[str, str]:
    """Build a `{name: type}` schema from a command's flags + positional args.

    Positionals come from the `use` string (cobra renders `<arg>` for required,
    `[arg]` for optional). Flags come from the explicit `flags` array.
    """
    schema: dict[str, str] = {}
    for token in _positional_tokens(cmd.get("use", "")):
        name, required = token
        schema[name] = "string" if required else "string?"
    for flag in cmd.get("flags", []) or []:
        name = flag.get("name")
        if not name:
            continue
        type_ = flag.get("type", "string")
        # All flags are optional in cobra unless explicitly marked required,
        # which the agent-context schema doesn't currently expose. Treat as
        # optional with a `?` suffix to match cocoon's existing convention.
        schema[name] = f"{type_}?"
    return schema


def _positional_tokens(use: str) -> list[tuple[str, bool]]:
    """Parse positional arg names from a cobra `use` string.

    `"items <itemId>"`              -> [("itemId", True)]
    `"items <itemId> [filter]"`     -> [("itemId", True), ("filter", False)]
    `"stories"`                     -> []
    """
    tokens: list[tuple[str, bool]] = []
    i = 0
    while i < len(use):
        c = use[i]
        if c in "<[":
            close = ">" if c == "<" else "]"
            end = use.find(close, i)
            if end == -1:
                break
            name = use[i + 1 : end].strip()
            if name:
                tokens.append((name, c == "<"))
            i = end + 1
        else:
            i += 1
    return tokens


def auth_mode(ctx: dict | None) -> str | None:
    """Read .auth.mode from an agent-context dict. Returns None when ctx is
    None or the field is missing — caller falls back to registry / default."""
    if ctx is None:
        return None
    auth = ctx.get("auth")
    if not isinstance(auth, dict):
        return None
    mode = auth.get("mode")
    return mode if isinstance(mode, str) else None


class AgentContextError(CocoonError):
    code = "agent_context_failed"
