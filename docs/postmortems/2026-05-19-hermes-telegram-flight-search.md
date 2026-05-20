# Postmortem: First real-world cocoon-on-hermes test

**Date:** 2026-05-19 (incident 1), 2026-05-20 (incidents 2–3)
**Author:** vu1n
**Status:** Draft — input for cocoon roadmap

## TL;DR

Cocoon was freshly installed on a hermes-on-VPS host (Debian 12) and registered as an MCP server. Over ~12 hours, three Telegram-driven interactions exposed four distinct failure modes:

1. **Flight search** (`Use cocoon to search flights da nang → shanghai`) — 10 cocoon calls in one turn, 110.9s elapsed, ended in `materialization_failed` because the cocoon MCP subprocess inherited a stale `PATH` without Go.
2. **Weather query** (`What's the weather in danang today` — no cocoon prefix) — agent reached for cocoon on its own to fill a host capability gap, found `open-meteo/*` catalog entries, and every call instantly died with a previously-unseen error: `no install_module`. The catalog row exists but is missing the field that tells cocoon how to materialize it. 5 wasted calls.
3. **"Use cocoon" follow-up** — same `no_install_module` errors, plus the model fell back to invoking `cocoon` via the bash terminal tool (MCP cocoon tool was in disabled state after a botched re-registration) and lost all the structured-error benefits of the typed MCP path.

**The steady-state UX is good** — every individual cocoon call returned in 0.06–0.37s. The failures clustered on (a) cold-start latency / toolchain dependencies, (b) catalog completeness, (c) host-environment plumbing, and (d) bash-fallback ergonomics. None are fundamental; all are addressable.

## Timeline (UTC-ish, single Telegram turn)

| Time | Event |
|---|---|
| 19:45:34 | Telegram inbound: "Use cocoon to search flights from da nang to Shanghai" |
| 19:45:36 | Agent turn begins (deepseek-v4-flash via Nous Portal) |
| 19:45:41 | `cocoon find` → 26 chars (0.37s) |
| 19:45:46 | `cocoon list` or `find` → 6510 chars (0.18s) |
| 19:45:47 | `cocoon describe` → 26 chars (0.16s) |
| 19:45:48 | `cocoon list` filtered → 39,019 chars (0.08s) |
| 19:45:52 | **Nous Portal 503**, retry in 2.8s |
| 19:45:58 | **Nous Portal 503**, retry in 5.0s |
| 19:46:10 | `cocoon` call → 3998 chars (0.18s) |
| 19:46:11 | `cocoon` call → 4956 chars (0.18s) |
| 19:46:15 | `cocoon call pointhound/search` → **`capability_not_found`** (0.09s) |
| 19:46:21 | `cocoon` call → 1310 chars (0.06s) |
| 19:46:26 | `cocoon` call → 11,338 chars (0.18s) |
| 19:46:31 | `cocoon call pointhound/...` → **`materialization_failed`: Go toolchain not installed** (0.08s) |
| 19:46:55 | **Nous Portal 503** (web_tools fallback) |
| 19:47:25 | Response sent to Telegram (873 chars, total 110.9s, 10 api_calls) |

## Impact

- **User experience:** A simple "search flights" prompt produced a 110-second wait and a graceful-failure response with no actual flights. Not a hang, but functionally a miss.
- **Cost:** 10 LLM tool-call cycles for what should've been 1–2; significant context churn (~70KB of cocoon output across the turn).
- **Trust:** This was the user's first Telegram-to-cocoon test. The pattern that worked architecturally failed perceptually.
- **Blast radius:** Single user, single turn, no data loss. Hermes/aeon/croptop unaffected.

## Subsequent interactions (added 2026-05-20)

Two more Telegram tests over the next ~12 hours surfaced two new failure modes that the flight-search incident didn't cover.

### Incident 2 — Weather query (02:03 UTC, 2026-05-20)

**User prompt:** "What's the weather in danang today" (no `Use cocoon` prefix — agent reached for cocoon on its own).

**Why cocoon at all:** There is no native weather skill or tool on this hermes host. The agent searched its toolspace, found cocoon's `open-meteo` catalog entries (5 endpoints: `forecast`, `forecast.get`, `air-quality.get`, `geocode.search`, `archive.get`), and tried each in turn.

**What happened:** Every single `call` against `open-meteo/*` failed with a previously-unseen error:

```
error: Catalog entry for 'open-meteo/forecast' has no install_module;
       cannot auto-install. Either run `go install <module>@latest`
       manually, or update the catalog.
```

This is a new failure mode — **not** `materialization_failed` (which means Go install attempt failed), **not** `capability_not_found` (which means the entry doesn't exist). The entry exists, `find` happily returns it, but the catalog row is missing the `install_module` field that tells cocoon what to run. Every call instantly dies before `materialize` even tries.

The agent burned 5 tool calls discovering this, one entry at a time, because the failure was per-endpoint rather than per-API.

### Incident 3 — "Use cocoon" follow-up (02:05 UTC, 2026-05-20)

**User prompt:** "Use cocoon" — almost certainly the user trying to force cocoon usage after the weather query produced nothing.

**Critical context — MCP tool state.** Cocoon's MCP server registration had been partially broken earlier (a host-side terminal line-wrap corrupted the `hermes mcp add --env PATH=…` command, leaving cocoon registered but with the *tool itself* disabled at the host's enable prompt). The model no longer had the typed `mcp_cocoon_cocoon` tool — so it **fell back to invoking `cocoon` via the bash terminal tool**.

**What happened:**

- Re-ran the same `open-meteo` calls → same `no_install_module` errors, now wrapped in `terminal returned error` envelopes (lossy)
- `cocoon list` (truncated to arxiv/recipe-goat in the visible log tail)
- `cocoon describe` invoked with one positional arg instead of two → argparse error: `the following arguments are required: tool`
- One terminal command hit the **180-second timeout** (`exit_code: 124`)
- Side-trip into unrelated failures: `gh: command not found` (host hygiene), `memory action 'search'` unknown (model guessing API), `skill_manage create cocoon` → "skill already exists" (model trying to make up for missing MCP tool by creating a skill)

The agent didn't hang or crash, but the turn produced a lot of motion and no useful output. The bash-fallback path is technically functional but agent-hostile — every error comes back as a string blob the model has to text-parse instead of a structured `{error, message, detail}` it can reason about.

## New root causes from these incidents

### RC5 — Catalog incompleteness: `no_install_module` is a distinct silent-failure mode

Some catalog entries exist (so `find` and `list` surface them) but lack the `install_module` field, so any `call` instantly fails with no recovery path. This is **worse** than `capability_not_found` because the agent has every reason to believe the call should work — `find` returned the match with full param schema.

`open-meteo/*` is the canonical example. Likely there are others. There's no host-side or catalog-side check today that catches this before runtime.

This is also a per-endpoint failure rather than per-API, so an agent that's "trying to find a working endpoint" will burn N tool calls discovering that all N endpoints share the same broken parent.

### RC6 — Bash-fallback path is functional but agent-hostile

When the MCP cocoon tool is unavailable (host-side misregistration, server crash, restart-in-progress), the model intelligently falls back to invoking the `cocoon` CLI via its terminal tool. This is admirable resilience, but it has two structural problems:

1. **Structured errors become string errors.** Cocoon's stable error codes (`auth_missing`, `capability_not_found`, `materialization_failed`, `no_install_module`) come back as stderr text wrapped in hermes's `terminal returned error` envelope. The model can sometimes parse this, but it's an ad-hoc string-matching exercise instead of a typed reaction.
2. **Argv shape is fragile.** `cocoon describe` requires *two* positionals (`api` and `tool`). The model passed one and got an argparse error. The MCP `cocoon(action="describe", api=…, tool=…)` form makes this impossible to get wrong because the schema is enforced.

### RC7 — Cocoon attracts queries it can't usefully answer when host has gaps

When the host lacks a natural tool for the user's intent (weather, in this case), cocoon becomes the default "maybe-it'll-work" attractor. The agent fans out across whatever catalog matches surface — and if those matches are broken or wrong-shaped (open-meteo missing `install_module`; pointhound is award flights not commercial), the user pays for the discovery.

This isn't strictly cocoon's bug — the host should have a weather skill. But it's worth naming because cocoon disproportionately bears the perceptual cost of host-side capability gaps. A bad cocoon turn looks like "cocoon is slow and broken" even when the underlying issue is "we have no weather tool."

## What went right

1. **MCP integration is sound.** `hermes mcp add cocoon` worked; the cocoon tool surfaced as `mcp_cocoon_cocoon`; the model invoked it without prompting friction beyond the user prefixing "Use cocoon to…".
2. **Cocoon's hot path is fast.** Every individual tool call returned in <0.4s. The sandbox + JSON dispatch + BM25 layer is not a latency problem.
3. **Structured errors worked.** `capability_not_found` and `materialization_failed` were returned with stable codes and the agent (sort of) handled them — it didn't loop on the same call.
4. **`cocoon doctor` lied honestly.** It reported Go found at `/usr/local/go/bin/go`, because its own PATH lookup includes that directory. The MCP subprocess didn't, which is the next finding.

## Root causes

### RC1 — Environment inheritance: cocoon MCP subprocess had a stale PATH

The hermes gateway was running before Go was installed. When `hermes mcp add cocoon` was run and the cocoon subprocess was spawned, it inherited the gateway's parent `PATH`, which did not include `/usr/local/go/bin`. Cocoon's internal `materialize.path_with_gobin()` finds Go at *its own* lookup time (works for `cocoon doctor` invoked directly), but the `subprocess.run(["go", "install", ...])` it issues inherits the parent process env's PATH.

**Why `.bashrc` didn't help:** `.bashrc` is sourced by interactive bash shells. Long-running daemons (hermes gateway) and non-interactive subprocesses (cocoon serve, spawned via stdio) never read it.

**Specific to this install order:** had Go been installed *before* the gateway last started, the gateway's env would have been correct from the start. The fragility is real but only bites on out-of-order installs and restarts. A proper systemd unit with an explicit `Environment=PATH=...` would mitigate, but cocoon shouldn't depend on hosts knowing that.

### RC2 — Cold-start: 20-second `go install` is hostile to chat ergonomics

`materialization_failed` is the spectacular failure mode of this, but even the "happy path" (Go works → `go install <api>-pp-cli@latest` → ~20s) is painful in a chat context. Telegram users don't tolerate 20-second pauses gracefully, and the agent has no good way to communicate "I'm installing software, this is normal."

This is the same wound dressed differently — when the toolchain isn't there, cold-start fails; when it is, cold-start is just slow.

### RC3 — Routing: BM25 found false-positive matches for an off-corpus query

Flight search is genuinely not in the printing-press corpus (no Skyscanner, no Kayak, no commercial-flight API). But BM25 returned non-empty results — most prominently `pointhound`, which is a *points/awards* flights API, not commercial fares. The model treated the matches as worth pursuing because cocoon returned them as matches.

The catalog has no "no good match" signal. Any non-empty find result tells the model "go ahead, try one." That triggers the fan-out we saw.

### RC4 — Compounding factor: Nous Portal 503s

Three Nous 503s during the turn added ~30s of retry latency on top of cocoon's troubles. Not cocoon's fault; would've added 30s of waiting even on a clean run. Worth noting because it inflated the "this is slow" perception.

## What we already learned about cocoon's fit

- **Cocoon's value is asymmetric.** Steady-state UX is excellent (sub-second, sandboxed, scoped auth). Cold-start UX is bad enough to dominate first impressions.
- **The "200 APIs available" pitch backfires for chat-first agents.** Model fan-out across many candidates is more expensive than the time saved by not having dedicated MCP servers for each.
- **Cocoon's sweet spot:** stable-schema operational APIs the user *names by name* — Linear issues, Slack DMs, Stripe lookups, Cloudflare DNS. Not open-ended search.

## Action items (ranked smallest-bet-first)

### P0 — Catalog hygiene + `find` confidence + installability filter (cocoon-side, ~2–3 evenings)

The single best ROI: prevent RC3-style fan-out and RC5-style silent breakage by making `find` only surface entries that are both confidently matched **and** actually installable. Three concrete pieces:

**(a) `find` confidence threshold (RC3).** Add `COCOON_FIND_MIN_SCORE` (default ~3.0 or whatever calibration shows) in `catalog.find_capability`. Below threshold → return `{matches: [], hint: "no high-confidence match for query; try `list` or a different phrasing"}`.

**(b) Installability filter (RC5).** `find` and `list` MUST filter out catalog entries lacking `install_module`. An entry the agent can't actually call has no business being suggested. Bonus: `cocoon doctor` should print a count of catalog entries skipped for missing `install_module` so the gap is visible.

**(c) Catalog audit + better metadata.**
- Audit catalog for entries with no `install_module` and either complete them or remove them. `open-meteo` is the canonical broken case from Incident 2.
- Audit descriptions for over-broad summaries that BM25 will over-rank. `pointhound` is the canonical case from Incident 1 — clarify "award/points flights only, not commercial fares".
- Add catalog-side `categories` field per API (e.g. `["payments", "messaging", "weather"]`) so filters work without substring guessing.
- Add explicit `anti_summary` / `not_for` field per API where the failure mode is predictable.

This is free signal — it tells us whether routing and catalog completeness were the dominant problems before sinking time into structural fixes (P1/P2). It also defends against future quiet-failure regressions when new APIs land in the catalog without complete metadata.

### P1 — Pre-built binaries from printing-press releases (cocoon + upstream, ~1 weekend)

Kills RC1 *and* RC2 in one architectural move. Today `materialize` runs `go install <module>@latest`, which downloads source and compiles on the target. Switching to download-prebuilt-tarball-from-GitHub-release would:

- Cut first-call latency from ~20s to ~2–3s
- Remove the Go toolchain dependency entirely (cocoon hosts no longer need Go 1.26+, just `curl` + `tar`)
- Make RC1's PATH problem irrelevant — no `go install` to PATH-trip on
- Reduce the cocoon install instructions from "install Go, install bwrap, install cocoon, register MCP" to "install bwrap, install cocoon, register MCP"

Coordination required: printing-press has to publish per-platform release binaries (`linux-amd64`, `darwin-arm64`, `darwin-amd64`). Most printing-press CLIs already have a GitHub Actions release flow — this would extend it. Cocoon's `materialize` becomes a `curl + sha256 + extract` flow with the same caching semantics.

### P2 — `cocoon prefetch <api>` + activity-mining job (cocoon: ~1 evening; aeon-side job: ~1 evening)

Even after P1, first call is still ~2–3s. For chat-first hosts where the user expects instant responses, *zero* cold-start is the right target. Predictive prefetch makes that possible for the long tail of APIs the user actually touches.

Concrete:
- `cocoon prefetch <api>` subcommand: identical to materialize-on-call but explicit; safe to run in a cron job.
- `cocoon prefetch --all-installed` for cache warming after a host restart.
- Optional: `cocoon prefetch --queue <file>` reading a JSONL queue of `{api: "...", reason: "..."}`.
- Aeon-side job (separate): mine recent agent conversations + GitHub/X activity for API mentions, intersect with cocoon's catalog, enqueue top-N for prefetch overnight. The aeon memory store already has the right signal sources.

The conversation-mining signal also feeds back into catalog hygiene — repeated "user asked about X but no cocoon match" events are direct evidence of catalog gaps.

### P3 — Document MCP-host env contract; emit better error for `materialization_failed`

If P1 lands, this becomes mostly moot. Until then:

- README addition: "If registering cocoon with an MCP host, pass `--env PATH=…` including `/usr/local/go/bin` and `$HOME/go/bin`. Host daemons do not source `.bashrc`."
- `materialization_failed` should include the specific missing binary path and the exact env-var fix in its `detail` field, not just "install Go 1.26+".
- `cocoon doctor` could explicitly print the PATH it's using and warn if it differs from what a fresh shell would have.

### P3b — Make the bash-fallback path agent-friendly (RC6, cocoon-side, ~1 evening)

When the MCP tool is unavailable, the agent falls back to invoking `cocoon` via terminal. Today the CLI defaults to human-formatted output for everything except errors-from-the-action-itself; this loses structure when hermes wraps it in a `terminal returned error` envelope.

Concrete:
- Add `COCOON_AGENT_MODE=1` env var (or detect via stdin-not-a-tty) that flips the CLI to always emit JSON on both stdout and stderr, including structured error codes for usage errors (argparse failures), so the bash-fallback caller can branch on `error_code` instead of grepping stderr.
- Document the bash-fallback path explicitly in the README/SKILL: "If the MCP tool is unavailable, the CLI takes identical operations as subcommands. Set `COCOON_AGENT_MODE=1` to get JSON-shaped errors."
- Map argparse exit codes to specific error codes (`invalid_arguments` etc.) so an agent that mis-shapes a call gets a usable structured response.

### P4 — LLM rerank on `find` (deferred)

Only worth doing if P0+P1+P2 don't fix routing quality. The test exposed *no-match* as the problem, not *mis-ordered match*. LLM rerank earns its keep when BM25 is almost right; here BM25 was just wrong because the corpus didn't have the thing the user wanted.

## Non-actions (worth naming so we don't keep relitigating)

- **Switch from Go to Node CLIs.** Misframes the bottleneck. The substrate isn't slow; the *distribution model* (download source + compile) is slow. P1 fixes this without a substrate change and without losing the printing-press corpus.
- **Tighten Telegram "Use cocoon to…" prompt template.** The model shouldn't need that prefix at all — it should pick cocoon based on the tool description when the user names an API. If P0 lands cleanly, this becomes a non-issue. (And Incident 2 confirms the model already routes to cocoon without the prefix — sometimes when it shouldn't.)
- **Rename `mcp_cocoon_cocoon` tool.** That's hermes's `mcp_<server>_<tool>` namespace artifact; renaming cocoon's MCP tool to something action-shaped (`call_api`, `try_api`) marginally helps the model reach for it but isn't load-bearing.
- **Try to prevent cocoon from being attractor for host-capability gaps (RC7).** Not cocoon's problem to solve. The right fix is on the host side — install a weather skill, install a Google Flights skill — so cocoon stops being asked questions it doesn't have answers to. Cocoon should however *not make the situation worse* by surfacing broken catalog entries (that's P0(b)).

## Open questions

1. **What's the right `COCOON_FIND_MIN_SCORE` floor?** Needs calibration against the current catalog. Probably want to instrument `find` to log scores for a week of real queries before picking a number.
2. **Pre-built binaries — does printing-press want to own that or should cocoon ship a bundled mirror?** Upstream ownership is cleaner; mirror is faster to ship if upstream is slow to adopt.
3. **Can `cocoon prefetch` run inside a per-call sandbox?** Today materialize runs unsandboxed because Go needs network + write to `$GOPATH`. Tarball download is more constrained and could plausibly run in a tighter sandbox.

## Appendix — raw signals

- Hermes log evidence: `~/.hermes/logs/agent.log` and `~/.hermes/logs/gateway.log` on the VPS; cocoon stderr at `~/.hermes/logs/mcp-stderr.log`
- All 10 cocoon tool calls completed; none hung or timed out
- `cocoon doctor` post-incident: bwrap ok, Go found at `/usr/local/go/bin/go`, sandbox available, 0 auth files
- User Telegram response: 873 chars, graceful-failure shape (no actual flight data)
