"""End-to-end smoke test for cocoon against a real downloaded CLI.

Runs four scenarios:
  1. Cold cache + direct call                 (download → exec)
  2. Warm cache + direct call                 (hit cache, exec)
  3. Warm cache + discovery → call            (find → call hits cache)
  4. Cold cache + discovery → call            (uninstall, find → download → exec)

Target API: hackernews (no auth required). Verifies the seamless-install
model: cocoon downloads the prebuilt binary on first use, caches it, and
re-uses the cache on subsequent calls — all triggered by an `action="call"`,
no separate install step.

Run from the cocoon/ directory:
    uv run python scripts/e2e_smoke.py
"""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def hackernews_bin(cache_dir: str) -> Path:
    """The cocoon-owned cache path for the hackernews binary. Mirrors
    `materialize._binary_path("hackernews")` but doesn't need to import
    cocoon (the cache dir is passed via COCOON_CACHE_DIR env)."""
    return Path(cache_dir) / "bin" / "hackernews" / "hackernews-pp-cli"


def uninstall(cache_dir: str) -> None:
    binary = hackernews_bin(cache_dir)
    if binary.exists():
        binary.unlink()


def banner(text: str) -> None:
    print(f"\n\033[1;36m=== {text} ===\033[0m")


def unwrap(result) -> object:
    """MCP call_tool returns CallToolResult with content list; tools return
    JSON-serialized text. mcp 1.x exposes the raw return value as
    `structuredContent` when available — preferred over parsing the text."""
    if result.isError:
        return {"isError": True, "content": result.content}
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        # FastMCP wraps list returns as {"result": [...]}; unwrap that.
        if isinstance(structured, dict) and list(structured.keys()) == ["result"]:
            return structured["result"]
        return structured
    return json.loads(result.content[0].text)


async def main() -> int:
    # Use a tempdir UNDER ~/.cache rather than tempfile's default (/var/folders
    # on macOS, /tmp on linux). macOS sandbox-exec refuses execvp() of binaries
    # under those scratch paths even with `(allow process-exec (literal ...))`
    # in the profile; ~/.cache/ is exec-allowed and matches the real user
    # cocoon cache path. Linux is fine either way; aligning anyway.
    cache_parent = Path.home() / ".cache"
    cache_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cocoon-e2e-", dir=cache_parent) as cache_dir:
        print(f"isolated cache dir: {cache_dir}")
        env = {
            **os.environ,
            "COCOON_CACHE_DIR": cache_dir,
        }
        return await _run(env, cache_dir)


async def _run(env: dict[str, str], cache_dir: str) -> int:
    bin_path = hackernews_bin(cache_dir)
    params = StdioServerParameters(command="cocoon", args=["serve"], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print(f"registered tools: {sorted(t.name for t in tools.tools)}")
            assert [t.name for t in tools.tools] == ["cocoon"], \
                f"expected single 'cocoon' tool, got {[t.name for t in tools.tools]}"

            async def call(**kwargs):
                return unwrap(await session.call_tool("cocoon", kwargs))

            # ---------------------------------------------------------------
            banner("Test 1 — cold cache + direct call (download → exec)")
            assert not bin_path.exists(), "tmp cache should start empty"
            parsed = await call(
                action="call", api="hackernews", tool="doctor", args={"agent": True},
            )
            print(json.dumps(parsed, indent=2)[:500])
            assert bin_path.exists(), "cocoon should have downloaded the binary"
            assert parsed.get("exit_code") == 0, f"test 1 failed: {parsed}"

            # ---------------------------------------------------------------
            banner("Test 2 — warm cache + direct call (hit cache, exec)")
            mtime_before = bin_path.stat().st_mtime_ns
            parsed = await call(
                action="call", api="hackernews", tool="doctor", args={"agent": True},
            )
            print(json.dumps(parsed, indent=2)[:300])
            assert bin_path.stat().st_mtime_ns == mtime_before, \
                "binary was re-downloaded (cache miss)"
            assert parsed.get("exit_code") == 0

            # ---------------------------------------------------------------
            banner("Test 3 — warm cache + discovery → call")
            hits = await call(action="find", query="hacker news health check")
            print("top hit:", json.dumps(hits[0], indent=2))
            assert hits[0]["api"] == "hackernews"
            parsed = await call(
                action="call", api=hits[0]["api"], tool=hits[0]["tool"], args={"agent": True},
            )
            print(json.dumps(parsed, indent=2)[:300])
            assert parsed.get("exit_code") == 0

            # ---------------------------------------------------------------
            banner("Test 4 — cold cache + discovery → call (download)")
            uninstall(cache_dir)
            assert not bin_path.exists(), "uninstall failed"
            hits = await call(action="find", query="search hacker news stories")
            print("top hit:", json.dumps(hits[0], indent=2))
            assert hits[0]["api"] == "hackernews"
            parsed = await call(
                action="call", api="hackernews", tool="doctor", args={"agent": True},
            )
            print(json.dumps(parsed, indent=2)[:300])
            assert bin_path.exists(), "cocoon should have re-downloaded the binary"
            assert parsed.get("exit_code") == 0

    print("\n\033[1;32mall four tests passed\033[0m")
    return 0



if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
