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

## Agentic GEPA (subagent-driven, no API key)

The dspy/GEPA library wants a programmatic `dspy.LM` and a provider key. If the
host runs in a context with a calling agent + subagent capability (Claude Code,
Codex), the SAME reflective loop is implementable agentically — predictor and
reflector are both subagents, no separate key needed. The optimizable artifact
is `src/cocoon/discovery_prompt.md` (the routing instructions a subagent
follows; this is also the canonical copy `find` ships as `discovery.instructions`
on fall-through responses).

The loop:
1. **Predict:** spawn a (cheap) subagent — e.g. Haiku — with the discovery
   prompt + the index + the queries; collect predictions JSON.
2. **Score** via `run_discovery_eval.py --predictions <file>`; record outcomes.
3. **Reflect:** spawn a (stronger) subagent — Sonnet/Opus — with the current
   prompt + a curated failure set + the eval's textual feedback; propose a
   revised prompt that targets the dominant failure modes without regressing
   what worked. Write to `discovery_prompt_vN.md`.
4. **Re-predict + score** with the revised prompt; if it Pareto-beats the
   previous on the metrics that matter, promote it; else iterate.

### Result of two iterations (Haiku predictor, scaled n=259)

| | v0 (precision rule only) | v1 (added verification + domain check) | **v2 (Pareto-aware re-reflection)** |
|---|---|---|---|
| named_id            | 80/80 | 80/80 | 78/80 |
| named_alias routed  | 8/29  | 11/29 | **21/29** |
| named_alias misroutes | 8   | 4     | **1** |
| capability_unnamed routed | 3/90 | 5/90 | **57/90** |
| capability misroutes | 41   | 30    | **11** |
| bluffs              | 13    | 34    | **0** |
| **total confident-wrong** | 62 | 68 | **12** |
| **total routed correctly** | 91 | 104 | **156** |

v1 traded — it fixed misroutes but spiked bluffs (the "alias mid-sentence"
clause leaked into id-lexical-matching). v2's reflector diagnosed that
regression by name, preserved v1's verification step + different-domain rule,
restored v0's prose anti-examples (concrete in-prompt examples beat abstract
rules for weak models), and added a domain-check step. Result: **81% reduction
in confident-wrong rate (62 → 12), 71% increase in correct routes (91 → 156)**
— all without a single LM API key.

The current `src/cocoon/discovery_prompt.md` is v2 (promoted from the loop) and
ships with cocoon — `find` attaches it (alongside the compact index) to every
fall-through response. Run the predictor subagent again any time with new
failures to drive v3+. The same mechanism shipped as the dspy/GEPA library
when you want budget management, Pareto candidate tracking, or sample-efficient
automated rollouts.

## Optimize (GEPA via the dspy library)

The `[optimize]` extra wires the dspy discovery program + a GEPA optimize loop
against this metric — cocoon's *runtime* stays LM-free; this is offline only.

```sh
# one-time setup
uv sync --extra optimize
export DSPY_LM_MODEL=anthropic/claude-haiku-4-5     # any litellm model id
export ANTHROPIC_API_KEY=...                        # provider key
# optional: a stronger model for GEPA's reflection step
export DSPY_REFLECTION_LM=anthropic/claude-sonnet-4-6

# baseline the dspy strategy (Haiku) against this eval
uv run python scripts/eval/run_discovery_eval.py \
  --strategy dspy --dataset scripts/eval/discovery_dataset_scaled.jsonl

# GEPA-optimize the discovery program (trainset=scaled, valset=seed)
uv run python scripts/eval/optimize.py --auto light --out optimized.json
```

The optimize loop reuses the eval's score function as the GEPA metric, adding
textual feedback ("MISROUTE: routed X but gold is Y — the chosen api doesn't
fit; prefer falling through") so GEPA can reflect on *why* each failure
happened and rewrite the program's instructions accordingly. The optimized
program is what cocoon would ship for hosts that use the dspy discovery tier.
