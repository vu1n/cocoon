# Discovery eval

Turns assertions about cocoon's discovery into numbers, and lets any discovery
strategy compete on one metric — so we know whether a heavier strategy (an
LLM/subagent tier, a `dspy` module, GEPA optimization) actually earns its keep
before building it.

## Run

```sh
# baseline: the deterministic find() gate
uv run python scripts/eval/run_discovery_eval.py

# score externally-produced predictions (a subagent, a dspy module, …) on the
# same metric. predictions = JSON {query: {"status": "confident"|"fall_through",
# "apis": [...]}}
uv run python scripts/eval/run_discovery_eval.py --predictions preds.json
```

`discovery_dataset.jsonl` is corpus-grounded (every `gold_api` is validated
against the live catalog). Query classes: `named_id`, `named_alias`,
`capability_unnamed` (cocoon has a fit but the query doesn't name it),
`off_corpus` (must decline, incl. generic-word traps like "I need clarity").
`gold_api` may be a list — capability queries often have several valid APIs.

## What it measures

- **named routing accuracy** — does the gate route correctly when a service is named?
- **bluffs** — confident on an off_corpus query (false-positive precision risk)
- **misroutes** — confident to the *wrong* api on a named query (worst case)
- **blind-spot size** — capability/alias queries the strategy can't reach

## Result (current corpus, n=39)

Predictions from a gold-blind subagent given cocoon's index, scored on the same
metric as `find`, across two host-model strengths:

| | `find` (LM-free) | strong subagent | Haiku (weak) |
|---|---|---|---|
| named_id            | 20/20 | 20/20 | 20/20 |
| named_alias         | 0/2   | 2/2   | 2/2   |
| capability_unnamed  | 0/9 (blind) | 9/9 | 7/9 |
| bluffs / misroutes  | 0 / 0 | 0 / 0 | 1 / 0 |

**Conclusion.** The two-tier design holds, and is robust to host-model strength:
`find` is the fast, LM-free, high-precision path for *named* services; the
calling agent (or a subagent that searches the registry and returns only the
chosen tool — keeping the corpus out of the main agent's context) handles
explore/alias/capability. Even a *weak* model (Haiku) recovers most of `find`'s
blind spot (alias 2/2, capability 7/9) — far better than the gate's 0/2, 0/9.
cocoon hosts no LM.

The weak-vs-strong gap is small and specific — Haiku's 1 bluff was the
`apartments` generic-word trap (which `find`'s deterministic guard already
catches, so composing the guard under the LLM tier may close it for free) plus 2
non-obvious capability misses (`midjourney`, `spotify`). That ~8% is the measured
headroom for `dspy`/GEPA to optimize the SKILL instructions / `search_terms` /
index against this metric — worthwhile for weak hosts, not a prerequisite. The
`--predictions` interface is the rail: a weak model, a subagent, or a `dspy`
module all plug in and compete on the same numbers.
