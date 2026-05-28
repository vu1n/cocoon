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

## Result — scaled set, n=259

A strong subagent generated 259 corpus-grounded queries across 17 categories
(named_id, named_alias, capability_unnamed, off_corpus including generic-word
traps). `find` runs deterministically; Haiku (weak host model) routes
gold-blind given the same index a subagent-discovery tier would see.

| | `find` (LM-free) | Haiku (weak host) |
|---|---|---|
| named_id            | 79/80 (98%) | 80/80 (100%) |
| named_alias         | 3/29        | 6/29 |
| capability_unnamed  | 1/90 (blind) | 7/90 |
| **misroutes** (confident → wrong api) | 3 | **56** |
| **bluffs** (confident on off_corpus)  | **11** | 0 |

**Conclusion.** `find` is *honestly blind* (falls through on alias/capability)
but precise on out-of-corpus modulo 11 bluffs from generic-word api ids the
deterministic guard doesn't cover. Haiku is the opposite — perfect precision on
out-of-corpus prose (0 bluffs) but **confidently routes to the *wrong* API on
~50% of capability queries**, often grabbing a high-frequency api like
`digitalocean` when uncertain.

That ~40% weak-host error rate (mostly confident-wrong-API on capability) is
the GEPA/DSPy headroom, now properly measured: reflective optimization of the
SKILL instructions / `search_terms` / index (offline, against this metric) is
the lever to teach better calibration — "when nothing clearly fits, fall
through." The `--predictions` interface is the rail; any producer plugs in.

**Note on scale.** A 39-query seed showed Haiku at near-ceiling (1 bluff, 2
missed) and made GEPA headroom look small. At n=259 the picture flips: weak
hosts misroute substantially, not bluff. Small evals can mislead on optimizer
value — scale matters.

The seed (`discovery_dataset.jsonl`, n=39) is kept for fast regression checks;
the scaled set (`discovery_dataset_scaled.jsonl`, n=259) is the primary signal.
