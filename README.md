# cocoon

MCP facade over the [printing-press](https://github.com/mvanhorn/cli-printing-press) CLI corpus.
Registers as one MCP server, exposes four meta-tools (`find_capability`,
`describe_capability`, `call_capability`, `list_apis`), lazily materializes
per-API CLIs on first use, executes each call in a per-invocation sandbox
with only that API's credentials scoped in.

The agent-facing protocol is documented in the
[`cocoon` skill](https://github.com/vu1n/claude-skills/tree/main/skills/cocoon).
This directory is the runtime.

## Install

```sh
uvx cocoon init --host claude-code   # register cocoon in ~/.claude/mcp.json
uvx cocoon doctor                    # check bwrap/sandbox-exec/printing-press
uvx cocoon auth linear --token lin_… # write per-API credentials
uvx cocoon serve                     # run the MCP server (init wires this up)
```

Requires Python 3.11+. For execution sandboxing: `bubblewrap` on Linux,
`sandbox-exec` (built-in) on macOS. For CLI materialization: `printing-press` on PATH.

## Layout

```
src/cocoon/
  server.py         MCP server (official sdk); registers the four meta-tools
  catalog.py        catalog fetch, parse, search dispatch, list/describe
  search.py         BM25 ranker (vendored, ~30 lines)
  materialize.py    printing-press subprocess + binary cache
  auth.py           per-API JSON credential files at ~/.cache/cocoon/auth/
  argv.py           dict -> CLI argv translation
  paths.py          centralized cache-path resolution (no side effects)
  errors.py         structured error types matching the skill's failure modes
  cli.py            cocoon {init,serve,auth,doctor,catalog}
  sandbox/
    policy.py       SandboxPolicy dataclass
    linux.py        bubblewrap execution
    macos.py        Seatbelt (sandbox-exec) execution
    __init__.py     platform dispatch
```

## Development

```sh
uv sync --extra dev
uv run pytest
```

Tests cover everything that runs without external dependencies (paths, auth,
catalog parsing/search, sandbox argv/SBPL construction, argv translation,
CLI doctor). End-to-end tests against real `printing-press` + `bwrap` are
out of scope for v0.

## Status

v0.2 — runnable skeleton with a tightened design pass. Outstanding:

- Semantic search (BM25 today; embeddings index over endpoint descriptions later)
- Egress allowlist via outbound proxy (Claude Code pattern) — v1.1
- Bring-your-own-OpenAPI-spec registration — v1.1 with codegen sandboxing
- Real catalog URL for the printing-press-library manifest (currently falls back to a small bundled dev catalog when `COCOON_CATALOG_URL` is unset)
