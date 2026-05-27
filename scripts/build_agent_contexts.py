"""Aggregate per-CLI capability info for every API in printing-press-library,
write the result to `data/registry.json` at the repo root.

This file is the cocoon-published catalog — fetched by cocoon's runtime at
first need (default URL: raw.githubusercontent.com/vu1n/cocoon/main/data/registry.json),
cached locally with 24h TTL. The wheel does NOT ship this; cocoon installs
stay small and the catalog refreshes nightly without cocoon releases.

Source per API: each CLI's `tools-manifest.json` in the printing-press-library
source tree (~109 of ~149 CLIs as of writing). Synthetic agent-context stub
for the rest so they still surface in find/list and can self-bootstrap when
called.

Output shape (list-of-dicts, matches dev_catalog.json so catalog.py treats
both uniformly):

    [
      {
        "api": "hackernews",
        "description": "...",
        "install_module": "github.com/mvanhorn/printing-press-library/.../hackernews-pp-cli",
        "auth_type": "none",
        "endpoints": [
          {"tool": "stories.top", "summary": "...", "params_schema": {...},
           "positionals": [], "argv_path": ["stories", "top"]},
          ...
        ]
      },
      ...
    ]

Endpoints are pre-flattened by walking the synthetic agent-context tree
(same logic cocoon's agent_context.to_capabilities uses for local caches),
so cocoon runtime can iterate without re-walking.

Usage:
  uv run python scripts/build_agent_contexts.py
  uv run python scripts/build_agent_contexts.py --only hackernews ahrefs
  uv run python scripts/build_agent_contexts.py --check
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import sys
import time
from pathlib import Path

import httpx

# Import cocoon's tree-walker so manifest harvesting and local-cache
# extraction produce identical capability shapes.
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from cocoon import agent_context  # noqa: E402

REGISTRY_URL = "https://raw.githubusercontent.com/mvanhorn/printing-press-library/main/registry.json"
RAW_BASE = REGISTRY_URL.rsplit("/", 1)[0]
OUT_PATH = Path(__file__).parent.parent / "data" / "registry.json"


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
            print("error: --only filtered out all requested apis", file=sys.stderr)
            return 1

    print(f"harvesting tools-manifest for {len(entries)} apis "
          f"(concurrency={args.concurrency})", file=sys.stderr)
    started = time.monotonic()

    catalog_entries: list[dict] = []
    real_count = 0
    synthetic_count = 0

    with cf.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {pool.submit(_build_catalog_entry, e): e for e in entries}
        for fut in cf.as_completed(futures):
            entry = futures[fut]
            try:
                cat_entry, source = fut.result()
            except Exception as exc:
                print(f"  [error] {entry['name']}: {exc}", file=sys.stderr)
                continue
            catalog_entries.append(cat_entry)
            if source == "manifest":
                real_count += 1
            else:
                synthetic_count += 1

    catalog_entries.sort(key=lambda e: e["api"])
    elapsed = time.monotonic() - started
    print(f"done: {real_count} with tools-manifest, {synthetic_count} synthetic, "
          f"{elapsed:.1f}s", file=sys.stderr)

    encoded = json.dumps(catalog_entries, indent=2) + "\n"

    if args.check:
        existing = OUT_PATH.read_text(encoding="utf-8") if OUT_PATH.exists() else ""
        if existing == encoded:
            return 0
        print(f"drift: {OUT_PATH} would change", file=sys.stderr)
        return 1

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(encoded, encoding="utf-8")
    print(f"wrote {OUT_PATH} ({OUT_PATH.stat().st_size} bytes, "
          f"{len(catalog_entries)} apis)", file=sys.stderr)
    return 0


def _build_catalog_entry(entry: dict) -> tuple[dict, str]:
    """Translate one registry entry into the catalog shape cocoon consumes.
    Returns (entry_dict, source) where source is "manifest" (real tools-manifest
    harvested) or "synthetic" (stub with just the agent-context endpoint)."""
    name = entry["name"]
    path = entry.get("path", "")
    install_module = (
        f"github.com/mvanhorn/printing-press-library/{path}/cmd/{name}-pp-cli"
        if path else None
    )
    mcp = entry.get("mcp") or {}

    try:
        manifest = _fetch_manifest(name, path)
        ctx = _manifest_to_agent_context(name, manifest)
        source = "manifest"
        auth_type = (manifest.get("auth") or {}).get("type", "none")
    except _HarvestError:
        ctx = _synthetic_context(name, entry)
        source = "synthetic"
        auth_type = mcp.get("auth_type", "none")

    # Flatten the cobra command tree into the endpoint dicts cocoon expects.
    capabilities = agent_context.to_capabilities(name, ctx)
    endpoints = [
        {
            "tool": cap["tool"],
            "summary": cap["summary"],
            "params_schema": cap["params_schema"],
            "positionals": list(cap.get("positionals", ())),
            "argv_path": list(cap.get("argv_path", ())),
        }
        for cap in capabilities
    ]

    cat_entry = {
        "api": name,
        "description": entry.get("description", ""),
        "install_module": install_module,
        "auth_type": auth_type,
        # Upstream's curated category (e.g. "payments") and discovery terms
        # (aliases + capability phrases, added in printing-press #743). cocoon
        # surfaces both in the browse index so the calling LLM can search the
        # registry itself. Kept verbatim; "" / [] when absent.
        "category": entry.get("category") or "other",
        "search_terms": entry.get("search_terms") or [],
        "endpoints": endpoints,
    }
    return cat_entry, source


def _fetch_registry(url: str) -> list[dict]:
    response = httpx.get(url, timeout=30, follow_redirects=True)
    response.raise_for_status()
    data = response.json()
    return data["entries"] if isinstance(data, dict) else data


def _fetch_manifest(name: str, path: str) -> dict:
    if not path:
        raise _HarvestError("no path in registry entry")
    url = f"{RAW_BASE}/{path}/tools-manifest.json"
    response = httpx.get(url, timeout=15, follow_redirects=True)
    if response.status_code == 404:
        raise _HarvestError("no tools-manifest.json")
    response.raise_for_status()
    try:
        return response.json()
    except json.JSONDecodeError as exc:
        raise _HarvestError(f"manifest not JSON: {exc}") from exc


def _manifest_to_agent_context(api: str, manifest: dict) -> dict:
    """tools-manifest.json → agent-context shape (cobra command tree)."""
    auth_type = (manifest.get("auth") or {}).get("type", "none")
    return {
        "schema_version": "2",
        "source": "tools-manifest",
        "cli": {"name": f"{api}-pp-cli", "description": manifest.get("description", ""),
                "version": "unknown"},
        "auth": {"mode": auth_type, "env_vars": []},
        "commands": _build_command_tree(manifest.get("tools", [])),
    }


_VERB_SUFFIXES = {"get", "list", "create", "update", "delete",
                  "post", "put", "patch", "set"}


def _build_command_tree(tools: list[dict]) -> list[dict]:
    by_root: dict[str, list[tuple[str | None, dict]]] = {}
    for tool in tools:
        root, rest = _split_root(tool["name"])
        by_root.setdefault(root, []).append((rest, tool))
    return [_emit_root_command(root, children) for root, children in by_root.items()]


def _split_root(name: str) -> tuple[str, str | None]:
    parts = name.split("_", 1)
    return parts[0], parts[1] if len(parts) > 1 else None


def _emit_root_command(root: str, children: list[tuple[str | None, dict]]) -> dict:
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


def _synthetic_context(name: str, entry: dict) -> dict:
    """Minimal agent-context for CLIs without a tools-manifest. find/list see
    the API by description; `call <api> agent-context` downloads the binary
    and triggers cocoon's post-install schema capture, which then replaces
    this stub with the real command tree."""
    mcp = entry.get("mcp") or {}
    return {
        "schema_version": "2",
        "source": "synthetic",
        "cli": {"name": f"{name}-pp-cli", "description": entry.get("description", ""),
                "version": "unknown"},
        "auth": {"mode": mcp.get("auth_type", "none"), "env_vars": mcp.get("env_vars", [])},
        "commands": [{
            "name": "agent-context",
            "use": "agent-context",
            "short": entry.get("description", "") +
                     " (no tools-manifest upstream; call agent-context to capture the real schema)",
            "annotations": {"pp:endpoint": "agent-context"},
            "flags": [],
        }],
    }


class _HarvestError(Exception):
    pass


if __name__ == "__main__":
    sys.exit(main())
