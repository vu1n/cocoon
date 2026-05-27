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

| | `find` (deterministic, LM-free) | subagent (calling-LLM tier, gold-blind) |
|---|---|---|
| named_id            | 20/20 | 20/20 |
| named_alias         | 0/2   | 2/2   |
| capability_unnamed  | 0/9 (blind by design) | 9/9 |
| bluffs / misroutes  | 0 / 0 | 0 / 0 |

**Conclusion.** The two-tier design holds: `find` is the fast, LM-free,
high-precision path for *named* services; the calling agent (or a subagent that
searches the registry and returns only the chosen tool — keeping the corpus out
of the main agent's context) handles explore/alias/capability, recovering
`find`'s entire blind spot with no precision loss. cocoon hosts no LM.

A *capable* calling model already near-ceilings here, so `dspy`/GEPA optimization
has little routing-quality headroom for strong hosts. Where it would earn its
keep — and the next experiment that gates building it — is running this same
eval with the **actual (possibly weak) host model** as the predictor: if a weak
model bluffs or misses, GEPA-optimizing the SKILL instructions / `search_terms`
/ index (offline, against this metric) is the lever. The `--predictions`
interface is the rail: a weak model, a subagent, or a `dspy` module all plug in.
