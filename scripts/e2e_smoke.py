"""End-to-end smoke test for cocoon against a real installed CLI.

Runs four scenarios:
  1. Direct call, binary already installed       (skip install, hit sandbox+exec)
  2. Discovery then call, binary installed       (find_capability → call_capability)
  3. Discovery then call, binary missing         (install → exec via discovery)
  4. Direct call, binary missing                 (install → exec without discovery)

Target API: hackernews (no auth required). Each test exercises a different
combination of (discovery used? / binary present?). The smoke test verifies
the seamless-install model: an uninstalled CLI gets installed and executed
in one user-visible step, without the agent doing anything special.

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

HACKERNEWS_BIN = Path.home() / "go" / "bin" / "hackernews-pp-cli"


def uninstall() -> None:
    if HACKERNEWS_BIN.exists():
        HACKERNEWS_BIN.unlink()


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
    with tempfile.TemporaryDirectory(prefix="cocoon-e2e-") as cache_dir:
        print(f"isolated cache dir: {cache_dir}")
        env = {
            **os.environ,
            "COCOON_CACHE_DIR": cache_dir,
            "PATH": os.environ.get("PATH", "") + os.pathsep + str(Path.home() / "go" / "bin"),
        }
        return await _run(env)


async def _run(env: dict[str, str]) -> int:
    params = StdioServerParameters(command="cocoon", args=["serve"], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print(f"registered tools: {sorted(t.name for t in tools.tools)}")

            # ---------------------------------------------------------------
            banner("Test 1 — direct call, binary already installed")
            if not HACKERNEWS_BIN.exists():
                print("(install hackernews-pp-cli first via go install)")
                return 1
            res = await session.call_tool("call_capability", {
                "api": "hackernews", "tool": "doctor", "args": {"agent": True},
            })
            parsed = unwrap(res)
            print(json.dumps(parsed, indent=2)[:500])
            assert parsed.get("exit_code") == 0, f"test 1 failed: {parsed}"

            # ---------------------------------------------------------------
            banner("Test 2 — find_capability then call, binary installed")
            search = await session.call_tool("find_capability", {
                "query": "hacker news health check",
            })
            hits = unwrap(search)
            print("top hit:", json.dumps(hits[0], indent=2))
            assert hits[0]["api"] == "hackernews"
            res = await session.call_tool("call_capability", {
                "api": hits[0]["api"], "tool": hits[0]["tool"], "args": {"agent": True},
            })
            parsed = unwrap(res)
            print(json.dumps(parsed, indent=2)[:300])
            assert parsed.get("exit_code") == 0

            # ---------------------------------------------------------------
            banner("Test 3 — discovery then call, binary MISSING (seamless install)")
            uninstall()
            assert not HACKERNEWS_BIN.exists(), "uninstall failed"
            search = await session.call_tool("find_capability", {
                "query": "search hacker news stories",
            })
            hits = unwrap(search)
            print("top hit:", json.dumps(hits[0], indent=2))
            assert hits[0]["api"] == "hackernews"
            res = await session.call_tool("call_capability", {
                "api": "hackernews", "tool": "doctor", "args": {"agent": True},
            })
            parsed = unwrap(res)
            print(json.dumps(parsed, indent=2)[:300])
            assert HACKERNEWS_BIN.exists(), "cocoon should have installed the binary"
            assert parsed.get("exit_code") == 0

            # ---------------------------------------------------------------
            banner("Test 4 — direct call, binary MISSING (seamless install, no discovery)")
            uninstall()
            assert not HACKERNEWS_BIN.exists()
            res = await session.call_tool("call_capability", {
                "api": "hackernews", "tool": "doctor", "args": {"agent": True},
            })
            parsed = unwrap(res)
            print(json.dumps(parsed, indent=2)[:300])
            assert HACKERNEWS_BIN.exists()
            assert parsed.get("exit_code") == 0

    print("\n\033[1;32mall four tests passed\033[0m")
    return 0



if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
