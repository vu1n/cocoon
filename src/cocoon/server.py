"""MCP server: registers the four cocoon meta-tools.

Tools mirror the agent-facing protocol documented in the skill at
skills/cocoon/SKILL.md. Internal CocoonErrors are caught at the tool
boundary and returned as `to_dict()` payloads so MCP clients see a
stable shape (`{error, message, detail}`) instead of opaque exceptions.

`call_capability` is async so it can emit an MCP log message before the
slow first-time materialization and run the blocking subprocess in a
worker thread. Hosts (Claude Code, etc.) surface those log messages as
progress indicators.
"""

import asyncio
import functools
import inspect
import json
from typing import Any, Callable

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
def find_capability(query: str, limit: int = 5) -> list[dict] | dict:
    """Search the printing-press catalog at the endpoint level.

    Returns ranked matches, each with `api`, `tool`, `summary`, and
    `params_schema`. The schema is included so the model can construct
    the call on the first try without a follow-up describe step.
    """
    return [catalog.to_dict(cap) for cap in catalog.find_capability(query, limit)]


@mcp.tool()
@_catch_cocoon_errors
def describe_capability(api: str, tool: str) -> dict:
    """Return full schema, summary, and metadata for one capability.

    Use when `find_capability`'s summary isn't enough — long-tail flags,
    enum values, response paging semantics.
    """
    return catalog.to_dict(catalog.describe_capability(api, tool))


@mcp.tool()
@_catch_cocoon_errors
def list_apis(filter: str = "") -> list[dict] | dict:
    """Enumerate APIs in the catalog, optionally filtered by substring."""
    return [catalog.to_dict(s) for s in catalog.list_apis(filter)]


@mcp.tool()
@_catch_cocoon_errors
async def call_capability(
    api: str,
    tool: str,
    args: dict | None = None,
    ctx: Context | None = None,
) -> dict:
    """Execute a capability against the live API.

    On first use for any `api`, materializes (codegen + build) the
    underlying CLI via printing-press. Subsequent calls hit the cached
    binary. Executes in a per-call sandbox with only this API's auth
    token scoped into the environment.
    """
    binary = cached_binary(api)
    if binary is None:
        if ctx is not None:
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
