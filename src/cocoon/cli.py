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
import shutil
import sys
from pathlib import Path
from typing import Sequence

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

    return parser


def _cmd_serve(args: argparse.Namespace) -> int:
    from .server import run
    run()
    return 0


def _cmd_init(args: argparse.Namespace) -> int:
    snippet = {"mcpServers": {"cocoon": COCOON_ENTRY}}
    if args.print_only:
        print(json.dumps(snippet, indent=2))
        return 0

    config_path = _host_config_path(args.host)
    _merge_mcp_entry(config_path, "cocoon", COCOON_ENTRY)
    print(f"registered cocoon with {args.host}: {config_path}")
    return 0


def _cmd_auth(args: argparse.Namespace) -> int:
    env: dict[str, str] = {}
    if args.token:
        env["TOKEN"] = args.token
    for pair in args.env:
        if "=" not in pair:
            print(f"error: --env {pair!r} must be KEY=VALUE", file=sys.stderr)
            return 2
        key, value = pair.split("=", 1)
        env[key.strip()] = value
    if not env:
        print("error: provide --token and/or --env KEY=VALUE at least once", file=sys.stderr)
        return 2
    path = auth_module.write_token_env(args.api, env)
    print(f"wrote {path} (mode 0600, keys: {', '.join(sorted(env))})")
    return 0


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


def _merge_mcp_entry(config_path: Path, name: str, entry: dict) -> None:
    data = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    data.setdefault("mcpServers", {})[name] = entry
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
