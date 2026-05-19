"""Aggregate per-CLI capability info for every API in printing-press-library,
write the result to src/cocoon/data/agent_contexts.json.

Source: each CLI's `tools-manifest.json` in the library source tree —
upstream already generates it at codegen time, so we just `gh api` it.
No binary execution, no Go toolchain, no upstream PR needed. Some
hand-rolled CLIs don't have a manifest (~29% of the corpus); those get
skipped for now (Phase 2 will add an `agent-context` runtime fallback).

The output is shaped like a raw `<binary> agent-context` dump so cocoon's
runtime code (agent_context.to_capabilities) is uniform across the
local-cache and bundled-aggregate paths.

Usage:
  uv run python scripts/build_agent_contexts.py                       # all
  uv run python scripts/build_agent_contexts.py --only hackernews ahrefs
  uv run python scripts/build_agent_contexts.py --check               # drift check
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import sys
import time
from pathlib import Path

import httpx

REGISTRY_URL = "https://raw.githubusercontent.com/mvanhorn/printing-press-library/main/registry.json"
RAW_BASE = "https://raw.githubusercontent.com/mvanhorn/printing-press-library/main"
OUT_PATH = Path(__file__).parent.parent / "src" / "cocoon" / "data" / "agent_contexts.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", nargs="+", help="Limit to these api names.")
    parser.add_argument("--check", action="store_true",
                        help="Exit non-zero if the on-disk file would change.")
    parser.add_argument("--concurrency", type=int, default=16,
                        help="Parallel HTTP workers (default 16; tools-manifest fetch is I/O-bound).")
    args = parser.parse_args()

    entries = _fetch_registry(REGISTRY_URL)
    if args.only:
        entries = [e for e in entries if e.get("name") in set(args.only)]
        if not entries:
            print(f"error: --only filtered out all requested apis", file=sys.stderr)
            return 1

    print(f"harvesting tools-manifest for {len(entries)} apis "
          f"(concurrency={args.concurrency})", file=sys.stderr)
    started = time.monotonic()

    results: dict[str, dict] = {}
    skipped: list[tuple[str, str]] = []

    with cf.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {pool.submit(_harvest_one, e): e for e in entries}
        for fut in cf.as_completed(futures):
            entry = futures[fut]
            name = entry["name"]
            try:
                ctx = fut.result()
            except _HarvestError as exc:
                skipped.append((name, str(exc)))
                continue
            results[name] = {
                "registry": _registry_slim(entry),
                "agent_context": ctx,
            }

    elapsed = time.monotonic() - started
    print(f"done: {len(results)} ok, {len(skipped)} skipped, {elapsed:.1f}s", file=sys.stderr)
    if skipped:
        print(f"  skipped (no tools-manifest.json): "
              f"{', '.join(sorted(n for n, _ in skipped[:8]))}"
              f"{'...' if len(skipped) > 8 else ''}", file=sys.stderr)

    payload = {
        "schema_version": 1,
        "source_registry": REGISTRY_URL,
        "entries": dict(sorted(results.items())),
    }
    encoded = json.dumps(payload, indent=2) + "\n"

    if args.check:
        existing = OUT_PATH.read_text(encoding="utf-8") if OUT_PATH.exists() else ""
        if existing == encoded:
            return 0
        print(f"drift: {OUT_PATH} would change", file=sys.stderr)
        return 1

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(encoded, encoding="utf-8")
    print(f"wrote {OUT_PATH} ({OUT_PATH.stat().st_size} bytes)", file=sys.stderr)
    return 0


def _registry_slim(entry: dict) -> dict:
    return {
        "name": entry["name"],
        "category": entry.get("category", ""),
        "api": entry.get("api", ""),
        "description": entry.get("description", ""),
        "path": entry.get("path", ""),
        "mcp": entry.get("mcp"),
    }


def _fetch_registry(url: str) -> list[dict]:
    response = httpx.get(url, timeout=30, follow_redirects=True)
    response.raise_for_status()
    data = response.json()
    return data["entries"] if isinstance(data, dict) else data


def _harvest_one(entry: dict) -> dict:
    name = entry["name"]
    path = entry.get("path", "")
    if not path:
        raise _HarvestError("no path in registry entry")

    url = f"{RAW_BASE}/{path}/tools-manifest.json"
    response = httpx.get(url, timeout=15, follow_redirects=True)
    if response.status_code == 404:
        raise _HarvestError("no tools-manifest.json")
    response.raise_for_status()
    try:
        manifest = response.json()
    except json.JSONDecodeError as exc:
        raise _HarvestError(f"manifest not JSON: {exc}") from exc

    return _manifest_to_agent_context(name, manifest)


def _manifest_to_agent_context(api: str, manifest: dict) -> dict:
    """Translate upstream's tools-manifest.json shape into the same shape
    a `<binary> agent-context` dump produces, so cocoon's runtime parser
    works uniformly across local caches and the bundled aggregate."""
    auth_type = (manifest.get("auth") or {}).get("type", "none")
    commands = [_tool_to_command(t) for t in manifest.get("tools", [])]

    return {
        "schema_version": "2",
        "source": "tools-manifest",
        "cli": {
            "name": f"{api}-pp-cli",
            "description": manifest.get("description", ""),
            "version": "unknown",
        },
        "auth": {
            "mode": auth_type,
            "env_vars": [],
        },
        "commands": commands,
    }


def _tool_to_command(tool: dict) -> dict:
    """One tools-manifest entry → one flat agent-context command with the
    `pp:endpoint` annotation set to the dotted form.

    Naming convention: tools-manifest uses `<resource>_<verb>` (e.g.
    `items_get`, `stories_top`); agent-context uses `<resource>.<verb>`
    (e.g. `items.get`). Translation = first underscore → dot."""
    name = tool["name"]
    endpoint = name.replace("_", ".", 1)
    last_segment = endpoint.split(".")[-1]

    positionals = [p for p in tool.get("params", []) if p.get("location") == "path"]
    flags = [_param_to_flag(p) for p in tool.get("params", []) if p.get("location") != "path"]

    use_parts = [last_segment] + [f"<{p['name']}>" for p in positionals]
    return {
        "name": last_segment,
        "use": " ".join(use_parts),
        "short": tool.get("description", ""),
        "annotations": {"pp:endpoint": endpoint},
        "flags": flags,
    }


def _param_to_flag(param: dict) -> dict:
    return {
        "name": param["name"],
        "type": param.get("type", "string"),
        "usage": param.get("description", ""),
        "default": "",
    }


class _HarvestError(Exception):
    pass


if __name__ == "__main__":
    sys.exit(main())
