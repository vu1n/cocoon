---
name: cocoon
description: Discover and call APIs from the printing-press corpus (Linear, Slack, GitHub, Stripe, and 40+ others) on demand without per-API install. Use when the agent needs to interact with a third-party API and doesn't already have a dedicated MCP server configured for it; cocoon lazily generates, sandboxes, and executes a CLI for any indexed API and returns compressed output. Skip when a tuned MCP server for the specific API is already wired up, or when a single curl would obviously do.
---

<what-to-do>

When you need to interact with a third-party API and no dedicated MCP tool exists for it, call the single `cocoon` MCP tool with one of four actions:

1. **Search**: `cocoon(action="find", query="send a message to a slack channel")` returns ranked `(api, tool, summary, params_schema)` matches.
2. **Inspect** (only if you need fuller schema than `find` returned): `cocoon(action="describe", api="slack", tool="chat.postMessage")`.
3. **Call**: `cocoon(action="call", api="slack", tool="chat.postMessage", args={"channel": "#general", "text": "hi"})`. cocoon downloads the prebuilt CLI binary on first use (one-time, ~2–3s — surfaced as an MCP log notification), caches it, executes in a per-call sandbox with only that API's token scoped in, and returns the result.
4. **Enumerate**: `cocoon(action="list", filter="payments")` to browse the catalog without semantic search.

You do not install CLIs ahead of time. You do not configure per-API MCP servers. The single `cocoon` tool is the entire MCP interface; in a terminal, the same operations are `cocoon find/describe/call/list` subcommands.

</what-to-do>

<supporting-info>

## The surface: one MCP tool, four actions

cocoon runs as a single MCP server registered with the host agent (Claude Code, Codex desktop, Hermes, opencode, any MCP-compatible host). It exposes **one** tool — `cocoon` — that dispatches on an `action` field. The agent never sees per-API tool fan-out, never pays the context cost of N×50 tool definitions, never goes through an install step. cocoon is a small Python server; it downloads the per-platform prebuilt `<api>-pp-cli` binary from printing-press-library's GitHub release on first use (~2–3s) and caches it under `~/.cache/cocoon/bin/<api>/`.

Install and register with your host:

```sh
uvx cocoon init                         # registers via `claude mcp add` (user scope)
uvx cocoon init --print                 # show the registration command instead of running it
# For non-PyPI local installs, override with --command:
cocoon init --command "$(which cocoon) serve"
```

Restart Claude Code after `init` and the `cocoon` tool appears.

### Bash-fallback mode

If the MCP tool is unavailable (host misregistration, server restart-in-progress), the agent can invoke `cocoon` via its terminal tool instead. Set `COCOON_AGENT_MODE=1` in the subprocess env to get structured JSON on stdout and stderr (including argparse-level errors as `{"error": "invalid_arguments", ...}` rather than free-text "the following arguments are required") so the agent can branch on stable error codes.

## The single tool: `cocoon(action, ...)`

The action enum drives dispatch; per-action fields are validated server-side. All actions return either a structured result or `{error, message, detail}` with a stable error code.

### `action="find"` — search the catalog

Fields: `query` (required), `limit` (default 5).

BM25 ranking across the catalog at the **endpoint level**, not the API level. Returns matches with the schema you need to make the call — no follow-up describe needed in most cases.

```
cocoon(action="find", query="create a linear issue with a title and description")
→ [
    {"api": "linear", "tool": "issues.create", "summary": "Create a new issue",
     "params_schema": {"title": "string", "description": "string?",
                       "team_id": "string", "assignee_id": "string?"}},
    ...
  ]
```

### `action="describe"` — full schema for one capability

Fields: `api` (required), `tool` (required).

Use when `find`'s summary isn't enough — long-tail flags, enum values, response paging semantics.

### `action="call"` — execute against the live API

Fields: `api` (required), `tool` (required), `args` (dict, optional).

First call to any API downloads the prebuilt `<api>-pp-cli` binary from printing-press-library's GitHub release (one-time, ~2–3s — surfaced as an MCP log notification) and caches it under `~/.cache/cocoon/bin/<api>/`. Subsequent calls hit the cached binary directly. Every invocation runs in a per-call sandbox with only that API's credentials scoped in.

Returns:

```
{"exit_code": 0,
 "json": {...}        # if stdout parsed as JSON
 # or "stdout": "..."  # if plain text
 "stderr": "..."}     # only when non-empty
```

stdout/stderr capped at 64KB with a truncation marker.

### `action="list"` — enumerate APIs

Fields: `filter` (substring, optional).

Browse the catalog. Useful when you want to see what's available without semantic search.

## Seamless install

The agent never takes a separate install step. When `action="call"` runs against an API cocoon hasn't cached locally, cocoon downloads the per-platform prebuilt binary from `https://github.com/mvanhorn/printing-press-library/releases/download/<api>-current/<api>-pp-cli-<os>-<arch>`, writes it to `~/.cache/cocoon/bin/<api>/<api>-pp-cli`, marks it executable, and runs it through the sandbox.

cocoon emits an MCP log notification (`downloading <api>-pp-cli (first call, ~2-3s)`) before the network fetch so hosts can show progress. The agent should expect occasional slow first-calls and **not** retry on timeout — the download is in flight.

The download runs unsandboxed in v0 (network + write to the cache directory). The curated upstream catalog + GitHub-HTTPS trust are the trust boundaries. Upstream's goreleaser is configured to publish a `checksums.txt` alongside the binaries but currently doesn't upload it; an upstream PR adding that would let cocoon do sha256 verification on each download.

## Auth scoping

Per-API credentials live at `~/.cache/cocoon/auth/<api>.json` as JSON:

```
{"env": {"LINEAR_TOKEN": "lin_abc123"}}
```

Files are mode 0600 (user-only). They are **never** loaded into the cocoon server's environment. Each `action="call"` reads the relevant API's file and passes only those env vars into the per-invocation sandbox. A compromised Linear CLI sees the Linear token and nothing else.

Configure with:

```sh
cocoon auth linear --token lin_abc123
cocoon auth stripe --env STRIPE_KEY=sk_… --env STRIPE_VERSION=2025-01-01
```

First call to an API for which no token is configured returns a structured `auth_missing` error with the env-var name and the exact `cocoon auth` command to run. The agent should surface this to the user rather than guessing.

This pattern is borrowed from [pillbox](https://github.com/vu1n/pillbox)'s one-command auth isolation, applied at per-call granularity instead of per-session.

## Diagnostics

```sh
cocoon doctor
```

Reports sandbox backend availability, catalog URL + cache status, count of cached binaries, and configured auth files. Any `auth_missing` / `sandbox_unavailable` / `materialization_failed` error should send you here first.

## Sandbox

Every CLI invocation runs in an isolated sandbox. cocoon picks the OS-native primitive:

| OS | Primitive | What it isolates |
|---|---|---|
| Linux | bubblewrap | user/pid/net namespaces, per-path filesystem policy |
| macOS | Seatbelt (`sandbox-exec` + dynamic SBPL) | filesystem, network, process spawning |
| Windows | not supported in v1 | — |

Both paths share one `SandboxPolicy` dataclass; each OS backend translates it to its primitive (mirrors Codex's approach).

**What's protected**:
- Filesystem: read-only access to the cached binary; no access to user files outside an explicit working directory mount
- Network: `--unshare-net` on Linux by default (off only with explicit per-API opt-in for v1.1's egress proxy)
- Processes: cannot spawn arbitrary child processes outside the sandbox
- Tokens: scoped per-invocation; compromise of one CLI doesn't leak other APIs' credentials

**What's NOT protected**:
- Prompt injection inside an API response — that's a model-level defense problem; cocoon passes the response through faithfully and it's the agent's job to not act on injected instructions.
- macOS `sandbox-exec` is officially deprecated by Apple; the API still works but its long-term availability is uncertain. Token scoping on macOS comes from controlling the subprocess `env` directly, not from Seatbelt.
- v1 cannot enforce per-host egress allowlist (bubblewrap's `--unshare-net` is all-or-nothing). v1.1 adds an outbound proxy following Claude Code's pattern.

When the host agent has its own sandbox (Claude Code's bwrap/Seatbelt boundary), cocoon's sandbox layers under it — defense in depth, since the cocoon MCP server is a separate process outside the host's boundary.

## When to use this skill

Use cocoon when:
- The agent needs to call a third-party API and there's no MCP server already configured for it.
- The agent is exploring "what could I do" against an unfamiliar API and wants to enumerate capabilities.
- Context budget matters — cocoon keeps four tools in context regardless of catalog size, and returns pre-compressed CLI output.

Do NOT use cocoon when:
- A purpose-built MCP server for the specific API is already configured (e.g. the official Linear MCP server). That server's tuned schemas and behavior will be better than a generated CLI's.
- The task is a single, obviously-shaped HTTP call where `curl` is shorter than the meta-tool invocation chain.
- The user explicitly wants direct CLI control over a pre-installed printing-press binary — they're past the "agent-mediated" use case.

## Design rationale

**Why an MCP facade over the CLI fleet, not over the per-API MCP servers printing-press also emits?** Printing-press's per-API MCP servers default to endpoint-mirror mode — one tool per endpoint. With 134 APIs in the catalog and tens of endpoints each, registering all of them would put thousands of tool definitions into context. Per printing-press's own docs, MCP responses also dump raw API JSON (paging, nested fields, everything) while the CLI pre-formats. cocoon gets both wins: MCP's typed-schema-up-front benefit (the agent sees one `cocoon` tool with a small action enum), CLI's response compression (35-100× fewer tokens per call), zero context cost from unused APIs.

**Why one MCP tool instead of four (find/describe/call/list)?** Four tools is four schema blobs the agent loads up-front for what's effectively four overloads on the same operation. Action-multiplexing keeps one tool definition in context, with the per-action fields validated server-side. The CLI exposes the same operations as separate subcommands (`cocoon find`, `cocoon call`, etc.) since shell argv is naturally one verb-per-command.

**Why per-call sandbox instead of a long-lived sandboxed container?** A long-lived container is stateful infrastructure the host agent has to babysit (especially Hermes-style persistent agents). bwrap/Seatbelt invocation is millisecond-scale, so per-call gives equivalent ergonomics without lifecycle headaches and with a cleaner blast-radius boundary.

**Why curated catalog only in v1, no bring-your-own OpenAPI spec?** Running printing-press's codegen on an adversary-controlled spec is a code-injection vector. v1 trusts the curated corpus; v1.1 can add `register_api(spec_url)` with stricter codegen sandboxing once the threat model for that path is worked out.

**Why BM25 search instead of embeddings?** Endpoint summaries are short (≤ ~15 tokens), the catalog is bounded, and BM25's term-rarity weighting maps well to "Stripe charges" picking the charges endpoint over a generic list endpoint. Embeddings get added when there's a real corpus to tune against; ad-hoc embeddings on hundreds of short summaries underperform what an honest reader expects.

**Why not just register N printing-press MCP servers and call it done?** Context bloat (above) + every API needs separate auth config + the host UI shows thousands of tools instead of the one the agent actually needs + every spec update means a per-server restart. Aggregation into one meta-server isn't a stylistic choice; it's the only shape that scales past a handful of APIs.

## Failure modes (and what the agent should do)

| Symptom | Probable cause | Agent should |
|---|---|---|
| `materialization_failed` error | binary download failed (404 on the release asset, network unreachable, unsupported platform) | Surface the error to the user with the API name and the URL cocoon tried. Don't retry blindly — same input fails the same way. The detail payload includes `searched_path` / `url` / `platform` for diagnosis. |
| `auth_missing` error | No token configured for that API | Tell the user the exact `cocoon auth <api> --token …` command from the error payload. Don't proceed. |
| `sandbox_unavailable` warning | bwrap not installed (Linux) or Seatbelt missing (macOS) | cocoon refuses to execute. The agent should NOT suggest disabling the sandbox — refuse the task instead. |
| `capability_not_found` | Tool name doesn't exist for that API | Re-run `cocoon(action="find", ...)` or `action="list"` to confirm the exact name. |
| `catalog_unavailable` | `$COCOON_CATALOG_URL` set but unreachable | Suggest `cocoon catalog refresh` after the network is back, or unset the URL to use the bundled dev catalog. |
| Empty result on a search that should match | Catalog entry has sparse summaries | Use `action="list"` to confirm the API is in the catalog, then call directly with the tool name you'd expect. |
| Generated CLI returns a wall of Go stack trace | Spec drift or upstream printing-press bug | Report upstream; fall back to direct `curl` if the user is blocked. |

</supporting-info>

## Sources

This skill absorbed ideas from upstream skills/projects rather than depending on them at runtime. The `sources.json` file in this directory tracks each upstream with the SHA of its content at absorption time. A repo-level GitHub Action checks each source for drift on a weekly cron and opens a `@claude`-tagged PR when meaningful upstream changes appear.

- [`printing-press`](https://github.com/mvanhorn/cli-printing-press) — the CLI corpus and the dual CLI+MCP output model.
- [`pillbox`](https://github.com/vu1n/pillbox) — per-tool auth isolation.
- [Codex linux-sandbox](https://github.com/openai/codex/tree/main/codex-rs/linux-sandbox) — SandboxPolicy abstraction over OS primitives.
- [Claude Code sandboxing](https://code.claude.com/docs/en/sandboxing) — layered permissions and proxy-based egress allowlist.
