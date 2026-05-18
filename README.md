# cocoon

One MCP tool (and matching CLI) that lets an agent discover, auto-install, sandbox, and call any API in the [printing-press](https://github.com/mvanhorn/cli-printing-press) corpus — without per-API install steps, without per-API MCP server fan-out.

The agent-facing protocol is documented in [`skill/SKILL.md`](skill/SKILL.md). This repo holds both the runtime and the skill that ships with it.

## What it does

Register cocoon once with your MCP host. The agent then sees a **single tool**, `cocoon(action, ...)`, that multiplexes four operations:

```python
cocoon(action="find",     query="create a linear issue")
cocoon(action="describe", api="linear", tool="issues.create")
cocoon(action="call",     api="linear", tool="issues.create",
                          args={"title": "x", "team_id": "y"})
cocoon(action="list",     filter="payments")
```

On first `call` for any API, cocoon runs `go install <module>@latest` from the catalog entry, then executes the resulting `<api>-pp-cli` in a per-call sandbox (bubblewrap on Linux, Seatbelt on macOS) with only that API's credentials scoped into the environment. The agent never takes a separate install step.

The CLI mirrors the same operations as subcommands for terminal use:

```sh
cocoon find "create a linear issue"
cocoon describe linear issues.create
cocoon call linear issues.create --arg title=x --arg team_id=y
cocoon list --filter payments
```

## Install

```sh
uvx cocoon init                      # registers via `claude mcp add` (user scope)
uvx cocoon doctor                    # check sandbox + Go + catalog
uvx cocoon auth linear --token lin_… # write per-API credentials (mode 0600)
```

For a local install pointing at a checkout instead of PyPI:

```sh
cocoon init --command "$(which cocoon) serve"
# or, running from the repo:
cocoon init --command "uv run --directory /path/to/cocoon cocoon serve"
```

`cocoon init` shells out to `claude mcp add cocoon --scope user`, which writes the user-scope entry to `~/.claude.json`. (Older `~/.claude/mcp.json` is not read by modern Claude Code.) For other MCP hosts, use `cocoon init --print` to get both a shell command and a JSON snippet.

**Requirements**: Python 3.11+, Go 1.26+ (so cocoon can `go install` the printing-press CLIs), and `bubblewrap` (Linux) or `sandbox-exec` (built-in macOS) for execution sandboxing. `cocoon init` additionally needs the `claude` CLI on PATH.

## Layout

```
src/cocoon/
  server.py         MCP server: one `cocoon` tool dispatching on action
  cli.py            cocoon {serve, init, auth, doctor, catalog, find, describe, call, list}
  catalog.py        catalog fetch, BM25 search, list/describe, auth_type lookup
  search.py         BM25 ranker (vendored, ~30 lines)
  materialize.py    seamless `go install` of the per-API CLI, with PATH plumbing
  auth.py           per-API JSON credential files at ~/.cache/cocoon/auth/
  argv.py           dict -> CLI argv translation (dotted tool names → cobra subcommands)
  paths.py          centralized cache-path resolution (no side effects)
  errors.py         structured error types matching the skill's failure modes
  sandbox/
    policy.py       SandboxPolicy dataclass
    linux.py        bubblewrap execution
    macos.py        Seatbelt (sandbox-exec) execution
    __init__.py     platform dispatch + doctor probe

skill/
  SKILL.md          agent-facing protocol (what the model reads to learn cocoon)
  sources.json      upstream attributions for drift tracking

scripts/
  e2e_smoke.py      4-scenario end-to-end test against the real installed CLI

tests/              unit tests; no external deps (catalog/auth/sandbox/argv/CLI)
```

## Development

```sh
uv sync --extra dev
uv run pytest                                    # ~90 unit tests
uv run python scripts/e2e_smoke.py               # end-to-end against hackernews
```

The e2e script installs `hackernews-pp-cli` if missing (~20s on first run), then exercises the four scenarios: installed/direct, installed-via-discovery, uninstalled-via-discovery, uninstalled-via-direct-call.

## Status

v0.3 — single-tool MCP shape, seamless install, full CLI mirror, 91 unit tests, e2e proven against the real printing-press library. Outstanding:

- Production catalog source: today cocoon ships a small bundled dev catalog (5 APIs). Production wants the live `registry.json` from printing-press-library plus per-CLI endpoint schemas derived via `<api>-pp-cli agent-context`. Tracked in [#follow-up].
- Egress allowlist via outbound proxy (Claude Code pattern) — v1.1.
- Bring-your-own-OpenAPI-spec registration — v1.1 with codegen sandboxing.
- The `npx -y @mvanhorn/printing-press install` shortcut is upstream-broken (registry validation fails on a malformed entry); cocoon uses direct `go install` instead.
