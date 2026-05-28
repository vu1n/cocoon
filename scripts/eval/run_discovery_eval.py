"""Discovery eval: score a discovery strategy on labeled query→outcome data.

The point is to turn this session's assertions about `find` into numbers, and to
give any future optimizer (GEPA/MIPROv2) or alternative strategy (browse, an RLM
module) the SAME metric to compete on.

Each labeled example is one of four classes:
  named_id          query names the service by its catalog id ("...in linear")
  named_alias       query names it by an alias ("post on twitter" -> x-twitter)
  capability_unnamed cocoon HAS a fitting API but the query doesn't name it
                     ("send a text message" -> twilio) — `find` is blind here by
                     design; this class sizes the browse/RLM opportunity
  off_corpus        cocoon should NOT confidently claim it (incl. generic-word
                    traps like "I need clarity" — must not route to the clarity API)

A strategy maps query -> (status, apis) where status is "confident" | "fall_through".
We score each prediction against the gold and report the metrics that actually
matter for a reliable tier-gate:
  - routing accuracy on named queries (does the gate work when a service is named?)
  - BLUFF rate: confident on an off_corpus query (the false-positive precision risk)
  - MISROUTE rate: confident to the WRONG api on a named query (worst case)
  - blind-spot size: capability_unnamed queries find falls through (browse/RLM TODO)

Usage:
    uv run python scripts/eval/run_discovery_eval.py
    uv run python scripts/eval/run_discovery_eval.py --strategy find --json out.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from cocoon import catalog  # noqa: E402

DATASET = Path(__file__).resolve().parent / "discovery_dataset.jsonl"

# Outcome of scoring one example.
ROUTED = "routed_correct"   # confident → correct api
MISROUTE = "misrouted"      # confident → wrong api (false confident on a named query)
BLUFF = "bluffed"           # confident on an off_corpus query (should have declined)
MISSED = "missed"           # fell through when a real api exists (blind spot)
DECLINED = "declined"       # correctly fell through on off_corpus


def predict_find(query: str) -> tuple[str, set[str]]:
    """find strategy: confident iff the query names a service cocoon has."""
    r = catalog.find(query)
    if r.fall_through:
        return "fall_through", set()
    apis = {c.api for c in r.matches} or set(catalog._named_apis(query))
    return "confident", apis


def predict_dspy(query: str) -> tuple[str, set[str]]:
    """Lazy import — dspy is only required when this strategy is chosen
    (`--strategy dspy`), and only after the `[optimize]` extra is installed
    and an LM is configured via env. See scripts/eval/dspy_discovery.py."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from dspy_discovery import predict  # type: ignore[import-not-found]
    return predict(query)


STRATEGIES = {"find": predict_find, "dspy": predict_dspy}


def _gold_set(example: dict) -> set[str]:
    """gold_api may be a single id or a list — capability queries legitimately
    have several valid APIs (a lasagna recipe fits allrecipes OR food52)."""
    g = example["gold_api"]
    if g is None:
        return set()
    return set(g) if isinstance(g, list) else {g}


def score(example: dict, status: str, apis: set[str]) -> str:
    if example["gold_fall_through"]:
        return DECLINED if status == "fall_through" else BLUFF
    if status == "fall_through":
        return MISSED
    return ROUTED if (apis & _gold_set(example)) else MISROUTE


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--strategy", choices=sorted(STRATEGIES), default="find")
    ap.add_argument("--predictions", type=Path,
                    help="score externally-produced predictions (JSON {query: "
                         "{status, apis}}) — e.g. from a subagent or a dspy module — "
                         "instead of a built-in strategy. Lets any producer compete "
                         "on the same metric.")
    ap.add_argument("--dataset", type=Path, default=DATASET)
    ap.add_argument("--json", type=Path, help="write full per-example results here")
    args = ap.parse_args(argv)

    examples = [json.loads(line) for line in args.dataset.read_text().splitlines() if line.strip()]

    # Validate gold labels against the live catalog so a mislabeled api id (or one
    # that fell out of the corpus) is caught loudly rather than silently scored.
    catalog_apis = {e["api"] for e in catalog.load_catalog()}
    bad = sorted({a for ex in examples for a in _gold_set(ex) if a not in catalog_apis})
    if bad:
        print(f"WARNING: {len(bad)} gold api(s) not in catalog (fix labels): {bad}\n",
              file=sys.stderr)

    if args.predictions:
        preds = json.loads(args.predictions.read_text())
        strategy_name = f"external:{args.predictions.name}"

        def predict(query: str) -> tuple[str, set[str]]:
            p = preds.get(query)
            if not p:  # a producer that skipped a query is treated as a decline
                return "fall_through", set()
            return p["status"], set(p.get("apis", []))
    else:
        predict = STRATEGIES[args.strategy]
        strategy_name = args.strategy

    by_class: dict[str, Counter] = defaultdict(Counter)
    rows = []
    for ex in examples:
        status, apis = predict(ex["query"])
        outcome = score(ex, status, apis)
        by_class[ex["klass"]][outcome] += 1
        rows.append({**ex, "pred_status": status, "pred_apis": sorted(apis), "outcome": outcome})

    print(f"=== discovery eval: strategy={strategy_name}  n={len(examples)} ===\n")
    order = [ROUTED, MISROUTE, MISSED, BLUFF, DECLINED]
    for klass in ("named_id", "named_alias", "capability_unnamed", "off_corpus"):
        c = by_class.get(klass)
        if not c:
            continue
        total = sum(c.values())
        parts = "  ".join(f"{k}={c[k]}" for k in order if c[k])
        print(f"  {klass:<20} (n={total})  {parts}")

    # Headline metrics — the ones that decide whether find is a reliable gate.
    named = by_class["named_id"]
    named_total = sum(named.values()) or 1
    cap = by_class["capability_unnamed"]
    cap_total = sum(cap.values()) or 1
    bluffs = [r for r in rows if r["outcome"] == BLUFF]
    misroutes = [r for r in rows if r["outcome"] == MISROUTE]
    print("\n--- headline ---")
    print(f"  named_id routing accuracy : {named[ROUTED]}/{named_total} "
          f"({100*named[ROUTED]//named_total}%)")
    print(f"  alias recovery (named_alias): {by_class['named_alias'][ROUTED]}"
          f"/{sum(by_class['named_alias'].values())}")
    print(f"  capability_unnamed blind spot: {cap[MISSED]}/{cap_total} fell through "
          f"(the browse/RLM opportunity)")
    print(f"  BLUFFS (confident on off_corpus): {len(bluffs)}")
    print(f"  MISROUTES (named → wrong api): {len(misroutes)}")
    for r in bluffs + misroutes:
        print(f"      [{r['outcome']}] {r['query']!r} -> {r['pred_apis']} (gold={r['gold_api']})")

    if args.json:
        args.json.write_text(json.dumps(rows, indent=2) + "\n")
        print(f"\nwrote {args.json}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
