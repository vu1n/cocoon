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
RAW_BASE = REGISTRY_URL.rsplit("/", 1)[0]
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
    """Translate tools-manifest.json into an agent-context-shaped dump so
    cocoon's runtime parser is uniform across local caches and the bundle.

    Reconstructs the cobra command tree from the flat tools list. Without
    this, naive translation would emit each tool as a top-level command
    named after its verb suffix (e.g. `get` instead of `items`), and any
    runtime tree-walk would derive the wrong cobra invocation path.
    """
    auth_type = (manifest.get("auth") or {}).get("type", "none")
    return {
        "schema_version": "2",
        "source": "tools-manifest",
        "cli": {
            "name": f"{api}-pp-cli",
            "description": manifest.get("description", ""),
            "version": "unknown",
        },
        "auth": {"mode": auth_type, "env_vars": []},
        "commands": _build_command_tree(manifest.get("tools", [])),
    }


# Suffixes that, when used as the right-hand side of a `<resource>_<verb>` tool
# name, indicate the verb is a logical-name suffix on the bare resource
# command (so `items_get` → cobra `items <itemId>`, not `items get <itemId>`).
# Anything not in this set is a real nested cobra subcommand: `stories_top` →
# cobra `stories top`. The set is small and stable; if upstream adopts a new
# verb convention, an annotated endpoint here would get mis-emitted as a fake
# subcommand — surfaceable as a `capability_not_found` at invocation time.
_VERB_SUFFIXES = {"get", "list", "create", "update", "delete",
                  "post", "put", "patch", "set"}


def _build_command_tree(tools: list[dict]) -> list[dict]:
    """Group tools by their resource root, decide bare-root vs subcommand
    per the verb-suffix heuristic, emit nested command tree."""
    by_root: dict[str, list[tuple[str | None, dict]]] = {}
    for tool in tools:
        root, rest = _split_root(tool["name"])
        by_root.setdefault(root, []).append((rest, tool))

    return [_emit_root_command(root, children) for root, children in by_root.items()]


def _split_root(name: str) -> tuple[str, str | None]:
    parts = name.split("_", 1)
    return parts[0], parts[1] if len(parts) > 1 else None


def _emit_root_command(root: str, children: list[tuple[str | None, dict]]) -> dict:
    # A "default action" is a verb-suffix child that, by upstream convention,
    # invokes the bare root command (with positional if any). If present it
    # gets folded onto the root; non-verb-suffix children stay as subcommands.
    default_idx = next(
        (i for i, (rest, _t) in enumerate(children) if rest in _VERB_SUFFIXES),
        None,
    )
    if default_idx is not None:
        default = children[default_idx]
        others = children[:default_idx] + children[default_idx + 1:]
    else:
        default = None
        others = children

    root_cmd: dict = {"name": root}
    if default is not None:
        rest, tool = default
        positionals = _positionals(tool)
        root_cmd["use"] = " ".join([root] + [f"<{p['name']}>" for p in positionals])
        root_cmd["short"] = tool.get("description", "")
        root_cmd["annotations"] = {"pp:endpoint": f"{root}.{rest}"}
        root_cmd["flags"] = _flags(tool)
    else:
        root_cmd["use"] = root

    if others:
        root_cmd["subcommands"] = [_emit_subcommand(root, rest, tool) for rest, tool in others]
    return root_cmd


def _emit_subcommand(root: str, rest: str | None, tool: dict) -> dict:
    name = rest if rest is not None else tool["name"]
    positionals = _positionals(tool)
    return {
        "name": name,
        "use": " ".join([name] + [f"<{p['name']}>" for p in positionals]),
        "short": tool.get("description", ""),
        "annotations": {"pp:endpoint": f"{root}.{rest}" if rest else tool["name"]},
        "flags": _flags(tool),
    }


def _positionals(tool: dict) -> list[dict]:
    return [p for p in tool.get("params", []) if p.get("location") == "path"]


def _flags(tool: dict) -> list[dict]:
    return [_param_to_flag(p) for p in tool.get("params", []) if p.get("location") != "path"]


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
