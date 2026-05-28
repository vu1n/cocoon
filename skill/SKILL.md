---
name: cocoon
description: Execution runtime for structured operations against named third-party APIs (Linear, Slack, GitHub, Stripe, and 140+ others). Use when the user names a specific API and wants a typed operation against it (create issue, post message, charge card). Cocoon downloads the per-API CLI on demand and calls it through a per-call sandbox with locally-scoped credentials. SKIP for open-ended search queries (weather, news, flights, "find X") — native web_search beats cocoon there because no per-API credentials are needed. Cocoon's strength is structured, authenticated POST/PATCH/DELETE, not a competitor to web search.
---

<what-to-do>

When the user names a specific API and wants a structured operation against it, call the single `cocoon` MCP tool:

1. **Call** (the common case): `cocoon(action="call", api="slack", tool="chat.postMessage", args={"channel": "#general", "text": "hi"})`. Cocoon downloads the prebuilt CLI on first use (one-time, ~2–3s — surfaced as an MCP log notification), caches it, executes in a per-call sandbox with only that API's token scoped in, and returns the result.
2. **Find** is the unified discovery entry point: `cocoon(action="find", query="...")`. It is a two-tier resolver. If your query *names* a service cocoon has (Linear, Slack, …), the response carries `matches` you can call directly (`fall_through: false`). If your query *describes* a capability without naming a service ("send a text message"), the deterministic gate falls through (`fall_through: true`) and the response attaches **discovery rails**: a routing prompt + a compact registry index. Run the routing yourself against the rails (inline, or by spawning a subagent) — then call the resolved api. If your routing also falls through, the capability is genuinely off-corpus.
3. **Inspect** (only if a match's params summary isn't enough): `cocoon(action="describe", api="slack", tool="chat.postMessage")`.
4. **Browse** (corpus-shaped, not query-shaped): `cocoon(action="list")` for the category menu, then `cocoon(action="list", category="payments")` for a category's APIs. Use this when the user wants to see what's available without a specific question (e.g. "what can cocoon do for payments?"). For query-shaped lookups, prefer `find` — its discovery rails are the routing path; `list` is the manual fallback.

**When NOT to call cocoon at all:** for open-ended search queries — weather, news, flight scrapes, general "find me X" requests — prefer native `web_search` / `web_fetch`. Cocoon does best with structured, named-API operations against credentials the user has scoped in; it is not a web-search competitor.

You do not install CLIs ahead of time. You do not configure per-API MCP servers. The single `cocoon` tool is the entire MCP interface; in a terminal, the same operations are `cocoon find/describe/call/list/ready/auth` subcommands.

When `auth_status: "required"`, tell the user to run `cocoon auth <api>`. That dispatches by `auth_type`: for cookie / session APIs cocoon execs `<api>-pp-cli auth login --chrome` (the upstream CLI's own browser-cookie extractor, with encryption at rest under `~/.press-auth/`); for token APIs cocoon prompts for a paste and stores it under `~/.cache/cocoon/auth/<api>.json`. Cocoon orchestrates; the per-API CLI owns its credential lifecycle for browser-based flows.

</what-to-do>

<supporting-info>

## The surface: one MCP tool, four actions

cocoon runs as a single MCP server registered with the host agent (Claude Code, Codex desktop, Hermes, opencode, any MCP-compatible host). It exposes **one** tool — `cocoon` — that dispatches on an `action` field. The agent never sees per-API tool fan-out, never pays the context cost of N×50 tool definitions, never goes through an install step. cocoon is a small Python server; it downloads the per-platform prebuilt `<api>-pp-cli` binary from printing-press-library's GitHub release on first use (~2–3s) and caches it under `~/.cache/cocoon/bin/<api>/`.

Install and register with your host:

```sh
# PyPI distribution is `cocoon-mcp`; installed CLI is `cocoon`.
uvx --from cocoon-mcp cocoon init           # register via `claude mcp add` (user scope)
uvx --from cocoon-mcp cocoon init --print   # show the registration command without running
# For non-PyPI local installs, override with --command:
cocoon init --command "$(which cocoon) serve"
```

Restart Claude Code after `init` and the `cocoon` tool appears.

### Bash-fallback mode

If the MCP tool is unavailable (host misregistration, server restart-in-progress), the agent can invoke `cocoon` via its terminal tool instead. Set `COCOON_AGENT_MODE=1` in the subprocess env to get structured JSON on stdout and stderr (including argparse-level errors as `{"error": "invalid_arguments", ...}` rather than free-text "the following arguments are required") so the agent can branch on stable error codes.

## The single tool: `cocoon(action, ...)`

The action enum drives dispatch; per-action fields are validated server-side. All actions return either a structured result or `{error, message, detail}` with a stable error code.

### `action="find"` — the unified discovery entry point

Fields: `query` (required), `limit` (default 5), `ready_only` (default false).

`find` is a two-tier resolver, not a fuzzy search. Tier 1 is a deterministic
gate (high precision when the query names a service). When the gate falls
through, tier 2 hands you routing rails — a prompt + compact index — so you
do the capability/alias routing yourself instead of dead-ending.

**Tier 1 — named match.** The query names a service cocoon has:

```
cocoon(action="find", query="create a linear issue with a title and description")
→ {
    "fall_through": false,
    "reason": "cocoon has linear; closest tool(s) below",
    "matches": [
      {"api": "linear", "tool": "issues.create", "summary": "Create a new issue",
       "params_schema": {"title": "string", "team_id": "string",
                         "description": "string?", "assignee_id": "string?"},
       "auth_status": "required"}, ...
    ],
    "discovery": null
  }
```

Pick a match and `call` it.

**Tier 2 — discovery rails.** The query describes a capability without
naming a service ("send a text message"):

```
cocoon(action="find", query="send a text message")
→ {
    "fall_through": true,
    "reason": "no service in your query matches one cocoon has by name; route via the discovery rails below...",
    "matches": [...advisory lexical guesses, NOT a route...],
    "discovery": {
      "instructions": "# cocoon discovery prompt — v2\n...routing procedure + hard rules...",
      "index": "twilio [social-and-messaging] — SMS, voice, WhatsApp messaging | text-message, sms, mms\nslack [social-and-messaging] — Team chat and notifications | ...\n..."
    }
  }
```

When `discovery` is non-null, **route the query yourself**: read `instructions`,
scan `index`, pick an api id (or decline). Two equivalent shapes — pick whichever
your host supports:

- **Inline routing** — read the rails, decide in this turn, then call
  `describe`/`call` on the resolved api.
- **Subagent routing** — spawn a subagent (Haiku is enough; the prompt was
  optimized against it) with `instructions` + `index` + the user's query;
  get back `{status, api}` per the prompt's output format; proceed.

If your routing also returns `fall_through`, the capability is genuinely
off-corpus — escalate (build the integration; consider turning it into a
skill). Do NOT chase the `matches` list in tier-2 responses: it is explicitly
labeled advisory lexical guesses, not a route. Calibration showed BM25 alone
out-ranks real services with coincidental term overlap (`pushover` over
`slack` on a "slack" query).

**Branch on `fall_through` + `discovery`:**
- `fall_through: false` → tier-1 hit; use `matches`.
- `fall_through: true` + `discovery: {...}` → tier-2; route via the rails.
- `fall_through: true` + `discovery: null` → empty query / no rails to run.

Each match carries an `auth_status`: `"none"` (callable now), `"configured"`
(auth set up locally, callable), or `"required"` (needs a setup step —
surface it, don't call). Matches sort ready (`none`/`configured`) before
gated; `ready_only=true` hard-filters to callable-now.

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

### `action="list"` — browse the registry yourself

Fields: `category` (optional), `filter` (keyword, optional), `ready_only` (default false).

This is how you **search the registry with your own reasoning** instead of trusting a relevance score. Two levels, both compact:

**Bare `list`** → the category menu (the cheap top level):
```
cocoon(action="list")
→ {"categories": [{"category": "payments", "api_count": 9},
                  {"category": "social-and-messaging", "api_count": 14}, ...],
   "hint": "list(category=...) or list(filter=...) to drill in"}
```

**`list(category=...)` or `list(filter=...)`** → a compact API index to scan. `filter` matches name + description + curated `search_terms`:
```
cocoon(action="list", category="payments")
→ {"apis": [{"api": "stripe", "category": "payments",
             "description": "payments and billing",
             "search_terms": ["Dunning queue", "payout-reconcile", "customer-360"],
             "endpoint_count": 47, "auth_status": "required"}, ...]}
```

Read the descriptions + `search_terms` (upstream's curated discovery keywords) and pick the API yourself — your judgment beats the ranker. Then `describe`/`call` the tool on the API you chose. Each row carries `auth_status` so you know whether it's callable now or needs a setup step. This keeps your context small: a category menu, then one category's index, then one API's tools — never the whole corpus.

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

First call to an API for which no token is configured returns a structured `auth_missing` error with the env-var name, the API's required `auth_type` (`api_key` / `bearer_token` / `cookie` / etc.), and a `setup_hint` sized to the auth_type — for cookie APIs that's `cocoon auth <api>` with no args (opens browser, reads cookies after login); for token APIs it's `cocoon auth <api> --token <token>`.

Run `cocoon auth <api>` with no flags in an interactive shell to dispatch to the generic flow for that API's `auth_type`. Run with `--token` / `--env` to write non-interactively.

To preview what's callable now without searching, use `cocoon ready` (or `cocoon(action="list", ready_only=true)`) — it groups APIs by `no_auth` and `configured`. This is the natural starting point when you're not sure what to even attempt.

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
- Network: `--unshare-net` on Linux by default (off only with explicit per-API opt-in when a CLI needs egress)
- Processes: cannot spawn arbitrary child processes outside the sandbox
- Tokens: scoped per-invocation; compromise of one CLI doesn't leak other APIs' credentials

**What's NOT protected**:
- Prompt injection inside an API response — that's a model-level defense problem; cocoon passes the response through faithfully and it's the agent's job to not act on injected instructions.
- macOS `sandbox-exec` is officially deprecated by Apple; the API still works but its long-term availability is uncertain. Token scoping on macOS comes from controlling the subprocess `env` directly, not from Seatbelt.
- No per-host egress allowlist — bubblewrap's `--unshare-net` is all-or-nothing on Linux, and an outbound-proxy pattern (Claude Code does this) isn't implemented in this release.

When the host agent has its own sandbox (Claude Code's bwrap/Seatbelt boundary), cocoon's sandbox layers under it — defense in depth, since the cocoon MCP server is a separate process outside the host's boundary.

## When to use this skill

Use cocoon when:
- The user names a specific API (Linear, Slack, Stripe, GitHub, …) and wants a structured operation against it.
- The operation is a typed POST/PATCH/DELETE that wants compressed, schema-validated output instead of raw API JSON.
- The user is browsing what's set up locally (`cocoon ready`) to decide what's possible.

Do NOT use cocoon when:
- The user asks an open-ended question that web search can answer (weather, news, current events, flight scrapes). Native `web_search` / `web_fetch` cover those without the auth and install detour cocoon imposes.
- A purpose-built MCP server for the specific API is already configured (e.g. the official Linear MCP server). That server's tuned schemas and behavior will be better than a generated CLI's.
- The task is a single, obviously-shaped HTTP call where `curl` is shorter than the meta-tool invocation chain.
- The user explicitly wants direct CLI control over a pre-installed printing-press binary — they're past the "agent-mediated" use case.

## Design rationale

**Why an MCP facade over the CLI fleet, not over the per-API MCP servers printing-press also emits?** Printing-press's per-API MCP servers default to endpoint-mirror mode — one tool per endpoint. With 194 APIs in the catalog and tens of endpoints each, registering all of them would put thousands of tool definitions into context. Per printing-press's own docs, MCP responses also dump raw API JSON (paging, nested fields, everything) while the CLI pre-formats. cocoon gets both wins: MCP's typed-schema-up-front benefit (the agent sees one `cocoon` tool with a small action enum), CLI's response compression (35-100× fewer tokens per call), zero context cost from unused APIs.

**Why one MCP tool instead of four (find/describe/call/list)?** Four tools is four schema blobs the agent loads up-front for what's effectively four overloads on the same operation. Action-multiplexing keeps one tool definition in context, with the per-action fields validated server-side. The CLI exposes the same operations as separate subcommands (`cocoon find`, `cocoon call`, etc.) since shell argv is naturally one verb-per-command.

**Why per-call sandbox instead of a long-lived sandboxed container?** A long-lived container is stateful infrastructure the host agent has to babysit (especially Hermes-style persistent agents). bwrap/Seatbelt invocation is millisecond-scale, so per-call gives equivalent ergonomics without lifecycle headaches and with a cleaner blast-radius boundary.

**Why curated catalog only, no bring-your-own OpenAPI spec?** Running printing-press's codegen on an adversary-controlled spec is a code-injection vector. cocoon trusts the curated corpus; `register_api(spec_url)` would need stricter codegen sandboxing and a worked-out threat model for that path, which isn't in this release.

**Why BM25 search instead of embeddings?** Endpoint summaries are short (≤ ~15 tokens), the catalog is bounded, and BM25's term-rarity weighting maps well to "Stripe charges" picking the charges endpoint over a generic list endpoint. Embeddings get added when there's a real corpus to tune against; ad-hoc embeddings on hundreds of short summaries underperform what an honest reader expects.

**Why not just register N printing-press MCP servers and call it done?** Context bloat (above) + every API needs separate auth config + the host UI shows thousands of tools instead of the one the agent actually needs + every spec update means a per-server restart. Aggregation into one meta-server isn't a stylistic choice; it's the only shape that scales past a handful of APIs.

## Failure modes (and what the agent should do)

| Symptom | Probable cause | Agent should |
|---|---|---|
| `materialization_failed` error | binary download failed (404 on the release asset, network unreachable, unsupported platform) | Surface the error to the user with the API name and the URL cocoon tried. Don't retry blindly — same input fails the same way. The detail payload includes `searched_path` / `url` / `platform` for diagnosis. |
| `auth_missing` error | No token configured for that API | Surface the `setup_hint` from the payload to the user. For cookie APIs that's `cocoon auth <api>` (opens browser); for token APIs it's `cocoon auth <api> --token <token>`. The payload's `auth_type` tells you the credential class. Don't proceed with the call. |
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
