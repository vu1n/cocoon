"""MCP server: registers a single `cocoon` tool that dispatches on `action`.

Action-multiplexed shape keeps one tool definition in the agent's MCP
context (vs four typed wrappers) while letting find / describe / call /
list each take their natural fields. The same Python functions back the
CLI subcommands, so MCP and CLI surfaces stay in lockstep.

CocoonError raised anywhere downstream is caught at the tool boundary
and returned as `{error, message, detail}` so MCP clients get a stable
shape instead of opaque exceptions.

`call` is async so the dispatcher can emit an MCP log notification
before the slow first-time materialization and run the blocking
subprocess in a worker thread. Hosts (Claude Code, etc.) surface those
log messages as progress indicators.
"""

import asyncio
import functools
import inspect
import json
from typing import Any, Callable, Literal

from mcp.server.fastmcp import Context, FastMCP

from . import argv as argv_module
from . import catalog
from .auth import load_token_env
from .errors import CocoonError
from .materialize import cached_binary, materialize
from .paths import ensure_dirs
from .sandbox import SandboxPolicy, execute

MAX_OUTPUT_BYTES = 64 * 1024

mcp: FastMCP = FastMCP("cocoon")


def _catch_cocoon_errors(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Map CocoonError to its dict form so MCP clients get a stable shape."""
    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def awrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return await fn(*args, **kwargs)
            except CocoonError as exc:
                return exc.to_dict()
        return awrapper

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except CocoonError as exc:
            return exc.to_dict()
    return wrapper


@mcp.tool()
@_catch_cocoon_errors
async def cocoon(
    action: Literal["find", "describe", "call", "list"],
    api: str | None = None,
    tool: str | None = None,
    args: dict | None = None,
    query: str | None = None,
    limit: int = 5,
    filter: str = "",
    ctx: Context | None = None,
) -> dict | list[dict]:
    """Discover and call APIs from the printing-press corpus.

    action="find"      → search the catalog. Required: query. Optional: limit.
                         Returns ranked [{api, tool, summary, params_schema}, ...].
    action="describe"  → full schema for one capability. Required: api, tool.
                         Returns {api, tool, summary, params_schema}.
    action="call"      → execute a capability against the live API.
                         Required: api, tool. Optional: args (dict of CLI flags).
                         Auto-installs the underlying CLI via `go install` on first
                         use (one-time, ~20s; surfaced as an MCP log notification).
                         Returns {exit_code, json|stdout, stderr?}.
    action="list"      → enumerate APIs. Optional: filter (substring).
                         Returns [{api, description, endpoint_count}, ...].

    On error returns {error, message, detail} with a stable code
    (auth_missing, materialization_failed, capability_not_found, etc.).
    """
    match action:
        case "find":
            _require(action, query=query)
            return [catalog.to_dict(c) for c in catalog.find_capability(query, limit)]
        case "describe":
            _require(action, api=api, tool=tool)
            return catalog.to_dict(catalog.describe_capability(api, tool))
        case "list":
            return [catalog.to_dict(s) for s in catalog.list_apis(filter)]
        case "call":
            _require(action, api=api, tool=tool)
            return await do_call(api, tool, args, ctx)
        case _:
            raise CocoonError(f"unknown action '{action}'", action=action)


def _require(action: str, **fields: Any) -> None:
    missing = [name for name, value in fields.items() if value is None]
    if missing:
        raise CocoonError(
            f"'{action}' requires {' and '.join(missing)}",
            action=action,
            missing=missing,
        )


async def do_call(api: str, tool: str, args: dict | None, ctx: Context | None) -> dict:
    # Always go through materialize: it returns fast when the binary is
    # already on PATH, and only then triggers the slow install path. Going
    # through it unconditionally ensures the agent-context cache stays in
    # sync with the installed binary (post-install enrichment fires whether
    # this is the first call or the user installed manually before cocoon).
    if cached_binary(api) is None and ctx is not None:
        await ctx.info(f"materializing {api} CLI (first call, can take ~30s)")
    binary = await asyncio.to_thread(materialize, api)

    env = {} if catalog.auth_type(api) == "none" else load_token_env(api)
    policy = SandboxPolicy(
        binary=binary,
        argv=argv_module.tool_argv(tool, args),
        env=env,
        network=True,
    )
    result = await asyncio.to_thread(execute, policy)
    return _format_result(result.returncode, result.stdout, result.stderr)


def _format_result(exit_code: int, stdout: str, stderr: str) -> dict[str, Any]:
    """Try JSON-decode stdout; cap stdout/stderr at MAX_OUTPUT_BYTES."""
    out: dict[str, Any] = {"exit_code": exit_code}
    parsed = _try_json(stdout)
    if parsed is not None:
        out["json"] = parsed
    else:
        out["stdout"] = _cap(stdout)
    if stderr:
        out["stderr"] = _cap(stderr)
    return out


def _try_json(text: str) -> Any | None:
    stripped = text.strip()
    if not stripped or stripped[0] not in "{[":
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None


def _cap(text: str) -> str:
    data = text.encode("utf-8")
    if len(data) <= MAX_OUTPUT_BYTES:
        return text
    head = data[:MAX_OUTPUT_BYTES].decode("utf-8", errors="ignore")
    return f"{head}\n…[truncated, {len(data) - MAX_OUTPUT_BYTES} bytes elided]"


def run() -> None:
    ensure_dirs()
    mcp.run()
