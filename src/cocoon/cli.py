"""cocoon command-line entry point.

Subcommands:
  serve              Run the MCP server (stdio transport).
  init --host NAME   Write cocoon into a host agent's MCP config (or --print).
  auth API ...       Write per-API credentials to ~/.cache/cocoon/auth/.
  doctor             Report bwrap/sandbox-exec/printing-press/catalog status.
  catalog refresh    Force-refresh the on-disk catalog cache.
"""

import argparse
import json
import os
import shlex
import shutil
import sys
from pathlib import Path
from typing import Callable, Sequence

from . import __version__
from . import auth as auth_module
from . import catalog as catalog_module
from .errors import CocoonError
from .paths import auth_dir, cache_root, catalog_dir, ensure_dirs
from .sandbox import probe as probe_sandbox

HOST_CONFIG_SUFFIXES: dict[str, tuple[str, ...]] = {
    "claude-code": (".claude", "mcp.json"),
    "hermes": (".hermes", "mcp.json"),
}

COCOON_ENTRY = {
    "command": "uvx",
    "args": ["cocoon", "serve"],
}


def _host_config_path(host: str) -> Path:
    """Resolve at call time so $HOME / Path.home() monkeypatching works."""
    return Path.home().joinpath(*HOST_CONFIG_SUFFIXES[host])


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "_handler", None)
    if handler is None:
        parser.print_help()
        return 2
    try:
        return handler(args) or 0
    except CocoonError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        if exc.detail:
            print(json.dumps(exc.detail, indent=2), file=sys.stderr)
        return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cocoon",
        description="Discover and call APIs from the printing-press corpus via MCP.",
    )
    parser.add_argument("--version", action="version", version=f"cocoon {__version__}")
    subs = parser.add_subparsers(dest="command")

    p_serve = subs.add_parser("serve", help="Run the cocoon MCP server (stdio).")
    p_serve.set_defaults(_handler=_cmd_serve)

    p_init = subs.add_parser("init", help="Register cocoon with a host agent.")
    group = p_init.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--host",
        choices=sorted(HOST_CONFIG_SUFFIXES),
        help="Write into this host's MCP config.",
    )
    group.add_argument("--print", dest="print_only", action="store_true",
                       help="Print the MCP snippet without writing any file.")
    p_init.add_argument(
        "--command",
        help=(
            "Override the MCP server invocation. Pass a shell-like string, e.g. "
            "\"$(which cocoon) serve\" for a local install or "
            "\"uv run --directory /path/to/cocoon cocoon serve\" for a checkout. "
            "Default is `uvx cocoon serve`, which requires cocoon to be on PyPI."
        ),
    )
    p_init.set_defaults(_handler=_cmd_init)

    p_auth = subs.add_parser("auth", help="Write per-API credentials.")
    p_auth.add_argument("api", help="API name (matches the catalog id).")
    p_auth.add_argument(
        "--token", help="Single TOKEN env var value. Shortcut for --env TOKEN=...",
    )
    p_auth.add_argument(
        "--env", action="append", default=[], metavar="KEY=VALUE",
        help="One env var to set (repeatable).",
    )
    p_auth.set_defaults(_handler=_cmd_auth)

    p_doctor = subs.add_parser("doctor", help="Diagnose runtime prerequisites.")
    p_doctor.set_defaults(_handler=_cmd_doctor)

    p_catalog = subs.add_parser("catalog", help="Catalog inspection / maintenance.")
    cat_sub = p_catalog.add_subparsers(dest="catalog_command", required=True)
    cat_refresh = cat_sub.add_parser("refresh", help="Force-refresh the catalog cache.")
    cat_refresh.set_defaults(_handler=_cmd_catalog_refresh)

    # Capability subcommands — mirror the MCP `cocoon` tool's actions.
    p_find = subs.add_parser("find", help="Search the catalog for capabilities.")
    p_find.add_argument("query", help="Natural-language description.")
    p_find.add_argument("--limit", type=int, default=5)
    p_find.add_argument("--json", dest="as_json", action="store_true",
                        help="Emit raw JSON instead of human-formatted output.")
    p_find.set_defaults(_handler=_cmd_find)

    p_describe = subs.add_parser("describe", help="Print full schema for one capability.")
    p_describe.add_argument("api")
    p_describe.add_argument("tool")
    p_describe.add_argument("--json", dest="as_json", action="store_true")
    p_describe.set_defaults(_handler=_cmd_describe)

    p_call = subs.add_parser("call", help="Execute a capability against the live API.")
    p_call.add_argument("api")
    p_call.add_argument("tool")
    p_call.add_argument("--arg", action="append", default=[], metavar="KEY=VALUE",
                        help="A single argument as KEY=VALUE (repeatable).")
    p_call.add_argument("--json-args", help="All arguments at once as a JSON object.")
    p_call.add_argument("--json", dest="as_json", action="store_true",
                        help="Emit the raw result JSON instead of formatted output.")
    p_call.set_defaults(_handler=_cmd_call)

    p_list = subs.add_parser("list", help="Enumerate APIs in the catalog.")
    p_list.add_argument("--filter", default="", help="Substring filter on name/description.")
    p_list.add_argument("--json", dest="as_json", action="store_true")
    p_list.set_defaults(_handler=_cmd_list)

    return parser


def _cmd_serve(args: argparse.Namespace) -> int:
    from .server import run
    run()
    return 0


def _cmd_init(args: argparse.Namespace) -> int:
    entry = _entry_from_command(args.command) if args.command else COCOON_ENTRY
    if entry is None:
        print("error: --command must be a non-empty shell string", file=sys.stderr)
        return 2

    snippet = {"mcpServers": {"cocoon": entry}}
    if args.print_only:
        print(json.dumps(snippet, indent=2))
        return 0

    config_path = _host_config_path(args.host)
    _merge_mcp_entry(config_path, "cocoon", entry)
    print(f"registered cocoon with {args.host}: {config_path}")
    print(f"  command: {entry['command']} {' '.join(entry['args'])}")
    return 0


def _entry_from_command(command: str) -> dict | None:
    parts = shlex.split(command)
    if not parts:
        return None
    return {"command": parts[0], "args": parts[1:]}


def _cmd_auth(args: argparse.Namespace) -> int:
    env: dict[str, str] = {}
    if args.token:
        env["TOKEN"] = args.token
    parsed = _parse_kv_pairs(args.env, flag="--env")
    if parsed is None:
        return 2
    env.update(parsed)
    if not env:
        print("error: provide --token and/or --env KEY=VALUE at least once", file=sys.stderr)
        return 2
    path = auth_module.write_token_env(args.api, env)
    print(f"wrote {path} (mode 0600, keys: {', '.join(sorted(env))})")
    return 0


def _parse_kv_pairs(pairs: list[str], *, flag: str) -> dict[str, str] | None:
    """Parse `["KEY=VALUE", ...]` to a dict. Returns None and prints to stderr
    on malformed input; the flag name is included in the error so the user
    knows which option was wrong."""
    out: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            print(f"error: {flag} {pair!r} must be KEY=VALUE", file=sys.stderr)
            return None
        key, value = pair.split("=", 1)
        out[key.strip()] = value
    return out


def _cmd_doctor(args: argparse.Namespace) -> int:
    from .materialize import path_with_gobin
    ensure_dirs()
    sandbox_info = probe_sandbox()
    go_path = shutil.which("go", path=path_with_gobin())
    catalog_url = os.environ.get("COCOON_CATALOG_URL") or "(unset; using bundled dev catalog)"
    auth_count = sum(1 for _ in auth_dir().glob("*.json"))
    catalog_cached = (catalog_dir() / catalog_module.CACHE_FILE).exists()

    print(f"cocoon {__version__}")
    print(f"cache root:       {cache_root()}")
    print(f"sandbox backend:  {sandbox_info['backend']} "
          f"({'ok' if sandbox_info['available'] else 'MISSING'})")
    if sandbox_info["available"]:
        print(f"                  {sandbox_info['path']}")
    print(f"go toolchain:     {go_path or 'MISSING (install Go 1.26+ from https://go.dev/dl/)'}")
    print(f"catalog url:      {catalog_url}")
    print(f"catalog cached:   {'yes' if catalog_cached else 'no'}")
    print(f"auth files:       {auth_count}")

    if not sandbox_info["available"] or go_path is None:
        return 1
    return 0


def _cmd_catalog_refresh(args: argparse.Namespace) -> int:
    data = catalog_module.refresh_catalog()
    print(f"refreshed catalog: {len(data)} apis")
    return 0


def _cmd_find(args: argparse.Namespace) -> int:
    results = [catalog_module.to_dict(c)
               for c in catalog_module.find_capability(args.query, args.limit)]
    def human() -> None:
        if not results:
            print("(no matches)")
            return
        for r in results:
            print(f"{r['api']}/{r['tool']}  —  {r['summary']}")
            if r["params_schema"]:
                print(f"    params: {r['params_schema']}")
    return _emit(results, args.as_json, human)


def _cmd_describe(args: argparse.Namespace) -> int:
    cap = catalog_module.to_dict(catalog_module.describe_capability(args.api, args.tool))
    def human() -> None:
        print(f"{cap['api']}/{cap['tool']}")
        print(f"  summary: {cap['summary']}")
        print(f"  params:  {cap['params_schema']}")
    return _emit(cap, args.as_json, human)


def _cmd_call(args: argparse.Namespace) -> int:
    import asyncio
    from .server import do_call
    call_args = _collect_call_args(args)
    if call_args is None:
        return 2
    result = asyncio.run(do_call(args.api, args.tool, call_args, ctx=None))

    if args.as_json:
        print(json.dumps(result, indent=2))
    elif "json" in result:
        print(json.dumps(result["json"], indent=2))
    elif "stdout" in result:
        print(result["stdout"])
    if result.get("stderr"):
        print(result["stderr"], file=sys.stderr)

    exit_code = result.get("exit_code")
    return exit_code if isinstance(exit_code, int) else 1


def _cmd_list(args: argparse.Namespace) -> int:
    summaries = [catalog_module.to_dict(s)
                 for s in catalog_module.list_apis(args.filter)]
    def human() -> None:
        for s in summaries:
            print(f"{s['api']:<20} {s['endpoint_count']:>3} endpoints  —  {s['description']}")
    return _emit(summaries, args.as_json, human)


def _emit(data: object, as_json: bool, format_human: Callable[[], None]) -> int:
    if as_json:
        print(json.dumps(data, indent=2))
    else:
        format_human()
    return 0


def _collect_call_args(args: argparse.Namespace) -> dict | None:
    if args.json_args:
        try:
            parsed = json.loads(args.json_args)
        except json.JSONDecodeError as exc:
            print(f"error: --json-args is not valid JSON: {exc}", file=sys.stderr)
            return None
        if not isinstance(parsed, dict):
            print("error: --json-args must decode to a JSON object", file=sys.stderr)
            return None
        return parsed
    return _parse_kv_pairs(args.arg, flag="--arg")


def _merge_mcp_entry(config_path: Path, name: str, entry: dict) -> None:
    data = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    data.setdefault("mcpServers", {})[name] = entry
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
