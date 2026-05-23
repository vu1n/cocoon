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
from . import auth_recipes
from . import catalog
from .auth import load_token_env
from .errors import AuthMissing, CocoonError
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
    ready_only: bool = False,
    ctx: Context | None = None,
) -> dict | list[dict]:
    """Execute structured operations against a named API the user has chosen.

    USE this tool when the user names a specific API and wants a structured
    operation against it ("create a Linear issue", "post to Slack channel
    #eng", "charge $50 on Stripe"). cocoon downloads the per-API CLI on
    demand and calls it through a per-call sandbox with credentials scoped
    in from local config.

    SKIP this tool for open-ended search queries — weather, news, flights,
    general "find X" requests. Native web_search / fetch tools cover those
    without requiring per-API credentials. cocoon's strength is structured,
    typed POST/PATCH/DELETE operations against authenticated APIs, not a
    competitor to web search.

    Each capability/API carries:
    - `auth_status`: "none" (callable immediately, e.g. hackernews) |
       "configured" (auth set up locally; callable) | "required"
       (needs `cocoon auth <api>` first — surface the setup step to
       the user instead of attempting the call).
    - `setup_recipe`: when cocoon ships setup guidance for the API,
       a dict with login_url, env_var, instructions, etc. Surface to
       the user verbatim. None when cocoon doesn't have a recipe yet.

    Find sorts ready capabilities first; pass `ready_only=true` to
    hard-filter to immediately-callable APIs only.

    action="find"      → BM25 search across the catalog. Required: query.
                         Optional: limit, ready_only. Returns ranked
                         [{api, tool, summary, params_schema, auth_status,
                           setup_recipe, score, ...}, ...] with ready
                         APIs first.
    action="describe"  → full schema for one capability. Required: api, tool.
    action="call"      → execute. Required: api, tool. Optional: args.
                         First call downloads the binary (~2-3s, surfaced
                         as an MCP log notification). Returns {exit_code,
                         json|stdout, stderr?}.
    action="list"      → enumerate APIs. Optional: filter (substring),
                         ready_only. Each row carries auth_status and
                         setup_recipe so the agent has everything it
                         needs to either call or guide the user through
                         setup, without a separate round-trip. (The CLI
                         has a `ready` subcommand that groups by status.)

    On error returns {error, message, detail} with a stable code
    (auth_missing, materialization_failed, capability_not_found, etc.).
    `auth_missing` payloads include `setup_method` (the same recipe
    embedded in list/describe) when cocoon has one for the API.
    """
    match action:
        case "find":
            _require(action, query=query)
            return [catalog.to_dict(c)
                    for c in catalog.find_capability(query, limit, ready_only=ready_only)]
        case "describe":
            _require(action, api=api, tool=tool)
            return catalog.to_dict(catalog.describe_capability(api, tool))
        case "list":
            return [catalog.to_dict(s)
                    for s in catalog.list_apis(filter, ready_only=ready_only)]
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
    # Resolve auth first — failing fast on a missing token saves the
    # slow binary-download path. The error payload includes auth_type
    # ("api_key" / "oauth" / "basic") so the caller knows what kind of
    # credential is needed instead of inferring from the message.
    api_auth_type = catalog.auth_type(api)
    if api_auth_type == "none":
        env: dict[str, str] = {}
    else:
        try:
            env = load_token_env(api)
        except AuthMissing as exc:
            exc.detail["auth_type"] = api_auth_type
            recipe = auth_recipes.recipe_for(api)
            if recipe is not None:
                exc.detail["setup_method"] = recipe
            raise

    # Always go through materialize: it returns fast when the binary is
    # already on PATH, and only then triggers the slow install path. Going
    # through it unconditionally ensures the agent-context cache stays in
    # sync with the installed binary (post-install enrichment fires whether
    # this is the first call or the user installed manually before cocoon).
    if cached_binary(api) is None and ctx is not None:
        await ctx.info(f"materializing {api} CLI (first call, can take ~30s)")
    binary = await asyncio.to_thread(materialize, api)
    positionals, argv_path = _invocation_for(api, tool)
    policy = SandboxPolicy(
        binary=binary,
        argv=argv_module.tool_argv(tool, args, positionals=positionals, argv_path=argv_path),
        env=env,
        network=True,
    )
    result = await asyncio.to_thread(execute, policy)
    return _format_result(result.returncode, result.stdout, result.stderr)


def _invocation_for(api: str, tool: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Catalog-declared (positionals, argv_path) for this tool, both () if
    the capability isn't in the catalog. Falling back to empty tuples
    preserves the flags-only / dot-split path for ad-hoc tools like
    `doctor` that work via cobra but aren't annotated `pp:endpoint`."""
    try:
        cap = catalog.describe_capability(api, tool)
    except CocoonError:
        return (), ()
    return cap.positionals, cap.argv_path


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
