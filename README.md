# cocoon

One MCP tool (and matching CLI) that lets an agent discover, auto-install, sandbox, and call any API in the [printing-press](https://github.com/mvanhorn/cli-printing-press) corpus — without per-API install steps, without per-API MCP server fan-out.

The agent-facing protocol is documented in [`skill/SKILL.md`](skill/SKILL.md). This repo holds both the runtime and the skill that ships with it.

> **Status: parked at 0.4.0a8 as a research vehicle.** The runtime works end-to-end (243 tests, scaled discovery eval at n=259, e2e proven against real GitHub-Release downloads), but the breadth-first 194-API design serves a usage pattern that didn't match the author's own. No active feature work; the techniques below are the deliverable. Issues / forks welcome.

## If you're here for…

- **Sandboxing Go CLIs on macOS** — `src/cocoon/sandbox/macos.py` builds SBPL using last-match-wins ordering so a deny-credentials rule can sit under a blanket allow. Scoped read allow-lists SIGABRT Go binaries; deny-creds is the working shape (postmortem: `scripts/conformance_probe.py` surfaced this calibration-first).
- **Agentic GEPA without an API key** — DSPy's GEPA optimizer pattern (predictor + reflector on textual feedback) implemented as subagent calls. Drops Haiku confident-wrong rate 24% → 5% on n=259 with no LM key. See [`scripts/eval/README.md`](scripts/eval/README.md) for the loop and result tables.
- **Two-tier discovery as an MCP pattern** — deterministic name-match gate + LLM-routing rails (prompt + compact index) attached on fall-through. Runtime stays LM-free; the host's LLM does the routing. See [`src/cocoon/catalog.py`](src/cocoon/catalog.py) (`FindResult` / `DiscoveryRails`) and [`src/cocoon/discovery_prompt.md`](src/cocoon/discovery_prompt.md).
- **Eval methodology for routing/discovery** — corpus-grounded synthetic queries (every gold validated against the live catalog), a `--predictions` rail so any producer competes on one metric, single-source-of-truth scoring shared between runner and GEPA metric. [`scripts/eval/`](scripts/eval/).

## What it does

Register cocoon once with your MCP host. The agent then sees a **single tool**, `cocoon(action, ...)`, that multiplexes four operations:

```python
# find is a two-tier resolver:
#   tier-1 (named match)  → fall_through=false, matches=[...], discovery=null
#   tier-2 (capability)   → fall_through=true,  discovery={instructions, index}
#                           — the caller's LLM routes against the rails
cocoon(action="find",     query="create a linear issue")     # tier-1
cocoon(action="find",     query="send a text message")        # tier-2

cocoon(action="describe", api="linear", tool="issues.create")
cocoon(action="call",     api="linear", tool="issues.create",
                          args={"title": "x", "team_id": "y"})
cocoon(action="list",     category="payments")               # manual browse
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
# `cocoon-mcp` is the PyPI distribution name; `cocoon` is the installed CLI.
uvx --from cocoon-mcp cocoon init                      # register via `claude mcp add`
uvx --from cocoon-mcp cocoon doctor                    # check sandbox + catalog state
uvx --from cocoon-mcp cocoon auth linear --token lin_… # write per-API credentials
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
  server.py             MCP server: one `cocoon` tool dispatching on action
  cli.py                cocoon {serve, init, auth, doctor, catalog, find, describe, call, list, ready}
  catalog.py            catalog fetch, BM25 + name-match, two-tier find, list/describe
  search.py             BM25 ranker (vendored, ~30 lines)
  materialize.py        download prebuilt `<api>-pp-cli` from GitHub Releases, cache under bin/
  auth.py               per-API JSON credential files at ~/.cache/cocoon/auth/
  auth_flows.py         per-auth_type setup orchestration (token paste, cookie via upstream CLI)
  agent_context.py      local cache of per-CLI capabilities (the agent-context view)
  argv.py               dict -> CLI argv translation (dotted tool names → cobra subcommands)
  conformance.py        sandbox-conformance probe ladder (synthetic_home → real_home → network)
  paths.py              centralized cache-path resolution (no side effects)
  errors.py             structured error types matching the skill's failure modes
  discovery_prompt.md   v2 routing prompt — packaged data, surfaced via find's discovery rails
  data/                 bundled offline-fallback catalog (5 APIs) for first-run-without-network
  sandbox/
    policy.py           SandboxPolicy dataclass (writable/readable/deny_read paths)
    linux.py            bubblewrap execution
    macos.py            Seatbelt (sandbox-exec) execution — SBPL last-match-wins
    __init__.py         platform dispatch + doctor probe

skill/
  SKILL.md              agent-facing protocol (what the model reads to learn cocoon)
  sources.json          upstream attributions for drift tracking

scripts/
  e2e_smoke.py          4-scenario end-to-end test against the real installed CLI
  conformance_probe.py  ladder probe across the catalog (which CLIs work in the sandbox)
  build_agent_contexts.py  refresh local agent-context cache by running CLIs in probe mode
  eval/                 discovery eval — `--predictions` rail, dspy/GEPA optional extra
    run_discovery_eval.py
    scoring.py          canonical 5-way classifier (runner + GEPA metric share it)
    dspy_discovery.py   dspy.Predict-based strategy (opt-in via `[optimize]`)
    optimize.py         GEPA wrapper around the discovery program
    discovery_dataset.jsonl         n=39 seed (fast regression)
    discovery_dataset_scaled.jsonl  n=259 scaled set (primary signal)

tests/                  243 unit tests; no external deps (catalog/auth/sandbox/argv/CLI)
```

## Development

```sh
uv sync --extra dev
uv run pytest                                    # 243 unit tests
uv run python scripts/e2e_smoke.py               # end-to-end against hackernews

# Optional: discovery eval + dspy/GEPA rails (offline-only; runtime stays LM-free)
uv sync --extra optimize
uv run python scripts/eval/run_discovery_eval.py
```

The e2e script installs `hackernews-pp-cli` if missing (~20s on first run), then exercises the four scenarios: installed/direct, installed-via-discovery, uninstalled-via-discovery, uninstalled-via-direct-call.

The discovery eval is the metric this codebase optimized against — see [`scripts/eval/README.md`](scripts/eval/README.md) for the agentic-GEPA loop (predictor + reflector as subagents) that took the shipping discovery prompt from v0 to v2.

## What landed (0.4.0a8)

- Single-tool MCP shape with action multiplexing (`find` / `describe` / `call` / `list` / `ready`).
- Seamless prebuilt-binary install (~2–3s cold-start vs the ~20s `go install` of v0.3).
- Full CLI mirror (`cocoon find`, `cocoon auth`, `cocoon doctor`, …).
- Per-call sandboxing with per-API credential scoping (bubblewrap / Seatbelt).
- Cookie-auth delegation to upstream's per-CLI `auth login --chrome` flow with content-hash snapshot-diff to scope only the changed press-auth files into subsequent calls.
- **Two-tier `find`**: deterministic name-match gate + LLM-routing rails (prompt + 194-line compact index) attached on fall-through. Eval-validated v2 prompt drops Haiku confident-wrong rate 24% → 5%.
- Discovery eval harness with `--predictions` rail, single-source-of-truth scoring, agentic GEPA pattern documented (no API key needed).
- 243 unit tests, e2e proven against real GitHub-Release downloads.
- 194 APIs in the published catalog (harvested from each CLI's `tools-manifest.json` plus `agent-context` backfill for manifest-less CLIs).

## Known gaps (not pursued)

The project is parked; these are recorded for anyone forking or extracting parts.

- **No sha256 verification on binary download.** Upstream's goreleaser is configured to publish `checksums.txt` but the upload step is missing. cocoon relies on GitHub-HTTPS trust today; an upstream PR adding the checksum upload would unblock verification.
- **No egress allowlist.** bubblewrap's `--unshare-net` is all-or-nothing on Linux; per-host allowlisting would need an outbound-proxy pattern (Claude Code does this). Not in this release.
- **No bring-your-own-OpenAPI-spec.** Running printing-press's codegen on adversary-controlled specs is a code-injection vector; trust is curated-corpus-only.
- **No real-query-log calibration.** `COCOON_FIND_MIN_SCORE` defaults to 0 with a warning rail for invalid env values; calibration against real traffic was on the roadmap but the architectural answer instead became "LLM-routing rails on fall-through" rather than tightening BM25.
- **No prefetch / warm-cache subcommand.** The cold-start is already ~2–3s per first call; activity-mining for warming was a P2 polish that didn't land.
