"""Aggregate per-CLI agent-context dumps for every API in the printing-press
library, write the result to src/cocoon/data/agent_contexts.json.

Run locally for development or by .github/workflows/build-agent-contexts.yml
on a cron. The resulting file ships with the wheel so a fresh cocoon
install has pre-install discovery against the full corpus — `cocoon find
"top hacker news stories"` returns the right tool before hackernews is
installed.

Failures (CLI doesn't build, agent-context not implemented yet, etc.) are
logged to stderr and skipped — the aggregated file is best-effort over
whatever the library currently has. Re-running on a warm Go cache picks
up new entries cheaply.

Usage:
  uv run python scripts/build_agent_contexts.py                    # all entries
  uv run python scripts/build_agent_contexts.py --only hackernews espn  # subset
  uv run python scripts/build_agent_contexts.py --check            # exit non-zero if file would change
  uv run python scripts/build_agent_contexts.py --concurrency 4    # parallel builds
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

REGISTRY_URL = "https://raw.githubusercontent.com/mvanhorn/printing-press-library/main/registry.json"
OUT_PATH = Path(__file__).parent.parent / "src" / "cocoon" / "data" / "agent_contexts.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", nargs="+", help="Limit to these api names.")
    parser.add_argument("--check", action="store_true",
                        help="Exit non-zero if the on-disk file would change.")
    parser.add_argument("--concurrency", type=int, default=4,
                        help="Parallel go-install workers (default 4).")
    args = parser.parse_args()

    go = shutil.which("go")
    if go is None:
        print("error: Go toolchain not on PATH", file=sys.stderr)
        return 1

    entries = _fetch_registry(REGISTRY_URL)
    if args.only:
        entries = [e for e in entries if e.get("name") in set(args.only)]
        if not entries:
            print(f"error: --only filtered out all {len(args.only)} requested apis", file=sys.stderr)
            return 1

    print(f"building agent-contexts for {len(entries)} apis "
          f"(concurrency={args.concurrency}, output={OUT_PATH})", file=sys.stderr)

    started = time.monotonic()
    results: dict[str, dict] = {}
    failures: list[tuple[str, str]] = []

    with cf.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {pool.submit(_build_one, go, e): e for e in entries}
        for fut in cf.as_completed(futures):
            entry = futures[fut]
            name = entry["name"]
            try:
                ctx = fut.result()
            except _BuildError as exc:
                failures.append((name, str(exc)))
                print(f"  [skip] {name}: {exc}", file=sys.stderr)
                continue
            results[name] = {
                "registry": {
                    "name": name,
                    "category": entry.get("category", ""),
                    "api": entry.get("api", ""),
                    "description": entry.get("description", ""),
                    "path": entry.get("path", ""),
                    "mcp": entry.get("mcp"),
                },
                "agent_context": ctx,
            }
            print(f"  [ok]   {name}", file=sys.stderr)

    elapsed = time.monotonic() - started
    print(f"done: {len(results)} ok, {len(failures)} skipped, {elapsed:.1f}s", file=sys.stderr)

    payload = {
        "schema_version": 1,
        "source_registry": REGISTRY_URL,
        "entries": results,
    }
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"

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


def _fetch_registry(url: str) -> list[dict]:
    response = httpx.get(url, timeout=30, follow_redirects=True)
    response.raise_for_status()
    data = response.json()
    return data["entries"] if isinstance(data, dict) else data


def _build_one(go: str, entry: dict) -> dict:
    name = entry["name"]
    path = entry.get("path", "")
    if not path:
        raise _BuildError("no path in registry entry")

    module = f"github.com/mvanhorn/printing-press-library/{path}/cmd/{name}-pp-cli"
    install = subprocess.run(
        [go, "install", f"{module}@latest"],
        capture_output=True, text=True, timeout=180,
    )
    if install.returncode != 0:
        raise _BuildError(f"go install failed: {_tail(install.stderr, 300)}")

    binary = Path.home() / "go" / "bin" / f"{name}-pp-cli"
    if not binary.exists():
        raise _BuildError(f"binary not at {binary} after install")

    # Some older CLIs may not implement agent-context yet; tolerate the
    # `unknown command` failure mode by treating any non-zero exit as a
    # skip. The agent-context subcommand is well-defined enough that real
    # success vs missing-subcommand is unambiguous.
    ctx_run = subprocess.run(
        [str(binary), "agent-context"],
        capture_output=True, text=True, timeout=30,
        env={"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", "/tmp")},
    )
    if ctx_run.returncode != 0:
        raise _BuildError(f"agent-context exit={ctx_run.returncode}: {_tail(ctx_run.stderr, 200)}")

    try:
        return json.loads(ctx_run.stdout)
    except json.JSONDecodeError as exc:
        raise _BuildError(f"agent-context returned non-JSON: {exc}") from exc


def _tail(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else "…" + text[-limit:]


class _BuildError(Exception):
    pass


if __name__ == "__main__":
    sys.exit(main())
