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

On first `call` for any API, cocoon downloads the per-platform prebuilt `<api>-pp-cli` binary from printing-press-library's GitHub release (tag `<api>-current`), caches it under `~/.cache/cocoon/bin/<api>/`, and executes it in a per-call sandbox (bubblewrap on Linux, Seatbelt on macOS) with only that API's credentials scoped into the environment. The agent never takes a separate install step.

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

**Requirements**: Python 3.11+, network access to GitHub Releases (cocoon downloads `<api>-pp-cli` binaries on first use), and `bubblewrap` (Linux) or `sandbox-exec` (built-in macOS) for execution sandboxing. `cocoon init` additionally needs the `claude` CLI on PATH. **No Go toolchain required** — prebuilt binaries are downloaded directly from upstream's release artifacts.

### Bash-fallback mode

If the MCP cocoon tool is unavailable for any reason (host-side misregistration, server restart-in-progress, hermes terminal-only mode), the agent can fall back to invoking the `cocoon` CLI directly via its terminal tool. Set `COCOON_AGENT_MODE=1` in the subprocess env to get structured JSON on stdout and stderr instead of human-formatted text — including argparse-level errors as `{"error": "invalid_arguments", ...}` rather than free-text "the following arguments are required". The agent can branch on stable error codes instead of grepping stderr.

## Layout

```
src/cocoon/
  server.py         MCP server: one `cocoon` tool dispatching on action
  cli.py            cocoon {serve, init, auth, doctor, catalog, find, describe, call, list}
  catalog.py        catalog fetch, BM25 search, list/describe, auth_type lookup
  search.py         BM25 ranker (vendored, ~30 lines)
  materialize.py    download prebuilt `<api>-pp-cli` from GitHub Releases, cache under bin/
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

v0.4 candidate — single-tool MCP shape, seamless prebuilt-binary install (~2–3s cold-start vs the ~20s `go install` of v0.3), full CLI mirror, 145 unit tests, e2e proven end-to-end against real GitHub Release downloads. The bundled catalog covers ~96 APIs (harvested from each CLI's published `tools-manifest.json`); a daily GitHub Action keeps it fresh.

Outstanding:

- ~39 CLIs in the upstream library lack a `tools-manifest.json` (hand-rolled CLIs without OpenAPI input). They're hidden from `find`/`list` via the installability filter. A Phase-2 build path running `<binary> agent-context` post-install could backfill them.
- Upstream doesn't publish `checksums.txt` alongside release binaries (goreleaser is configured for it but the upload step is missing). cocoon relies on GitHub-HTTPS trust today; an upstream PR adding the checksum upload would let cocoon do sha256 verification.
- `cocoon prefetch` subcommand + activity-mining for warm caches before the agent asks — postmortem P2.
- Calibrated `COCOON_FIND_MIN_SCORE` floor once we have real query logs.
- Egress allowlist via outbound proxy (Claude Code pattern) — v1.1.
- Bring-your-own-OpenAPI-spec registration — v1.1 with codegen sandboxing.
- The `npx -y @mvanhorn/printing-press install` shortcut is upstream-broken (registry validation fails on a malformed entry); not relevant for cocoon anymore since we don't shell out to npx or `go install`.
