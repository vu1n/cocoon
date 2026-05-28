"""GEPA-optimize the dspy discovery program against the discovery eval.

The eval metric is reused as the GEPA metric (with textual feedback added so
GEPA can reflect on *why* a route was wrong). The optimized prompt/few-shots
are what cocoon would ship for hosts that use the dspy discovery strategy.

Setup (one-time):

    export DSPY_LM_MODEL=anthropic/claude-haiku-4-5      # any litellm model id
    export ANTHROPIC_API_KEY=...                         # provider key
    # optional, defaults to DSPY_LM_MODEL:
    export DSPY_REFLECTION_LM=anthropic/claude-sonnet-4-6
    uv sync --extra optimize

Run:

    uv run python scripts/eval/optimize.py
    uv run python scripts/eval/optimize.py --auto medium --out artifact.json

The result is a saved program (JSON) you can re-load with
`program.load(path)` and run via `--predictions` in the eval, or ship as the
hosted artifact for the dspy discovery tier.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent / "src"))

from dspy_discovery import build_program  # noqa: E402

SEED = HERE / "discovery_dataset.jsonl"
SCALED = HERE / "discovery_dataset_scaled.jsonl"


def _load_examples(path: Path):
    """Load a discovery_dataset jsonl into dspy.Examples (lazy dspy import)."""
    import dspy
    out = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        ex = json.loads(line)
        out.append(dspy.Example(
            query=ex["query"],
            gold_api=ex["gold_api"],
            gold_fall_through=ex["gold_fall_through"],
            klass=ex["klass"],
        ).with_inputs("query"))
    return out


def _gold_set(example) -> set[str]:
    g = example.gold_api
    if g is None:
        return set()
    return set(g) if isinstance(g, list) else {g}


def metric_with_feedback(example, pred, trace=None, pred_name=None, pred_trace=None):
    """Score + actionable feedback for GEPA. Mirrors the eval's score(), but
    emits a textual feedback line GEPA can reflect on to rewrite the program's
    instructions. The feedback IS the optimization signal — be specific."""
    import dspy
    api = (getattr(pred, "api", "") or "").strip()
    gold_apis = _gold_set(example)
    gold_fall_through = example.gold_fall_through

    if gold_fall_through:
        if not api:
            return dspy.Prediction(score=1.0, feedback="Correct decline.")
        return dspy.Prediction(score=0.0, feedback=(
            f"BLUFF: you routed to {api!r}, but this query is out-of-corpus or "
            f"ordinary prose using a common word that happens to match an api id. "
            f"A word matching an api id is not a route signal — check whether the "
            f"api's description and search_terms actually serve what the user is "
            f"asking. Prefer falling through (empty api)."
        ))
    if not api:
        return dspy.Prediction(score=0.0, feedback=(
            f"MISSED: should have routed to one of {sorted(gold_apis)}. Re-read "
            f"those entries' descriptions and search_terms — the user's intent "
            f"matches that capability. Don't fall through when an api genuinely "
            f"fits."
        ))
    if api in gold_apis:
        return dspy.Prediction(score=1.0, feedback="Correct route.")
    return dspy.Prediction(score=0.0, feedback=(
        f"MISROUTE: you routed to {api!r}, but the gold api(s) are "
        f"{sorted(gold_apis)}. The chosen api's description/search_terms do not "
        f"fit this query. A confidently-wrong route is worse than declining — "
        f"when nothing clearly fits, fall through."
    ))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--trainset", type=Path, default=SCALED,
                    help="JSONL of examples to optimize against (default: scaled set).")
    ap.add_argument("--valset", type=Path, default=SEED,
                    help="Held-out eval for Pareto tracking (default: hand-curated seed).")
    ap.add_argument("--auto", choices=("light", "medium", "heavy"), default="light",
                    help="GEPA budget preset.")
    ap.add_argument("--out", type=Path, default=HERE / "optimized_discovery.json",
                    help="Where to save the optimized program.")
    args = ap.parse_args(argv)

    import dspy  # imported here so --help works without the extra installed
    program = build_program()  # also configures dspy.settings.lm from env

    trainset = _load_examples(args.trainset)
    valset = _load_examples(args.valset)
    print(f"trainset={len(trainset)}  valset={len(valset)}  auto={args.auto}", file=sys.stderr)

    reflection_model = os.environ.get("DSPY_REFLECTION_LM") or os.environ["DSPY_LM_MODEL"]
    optimizer = dspy.GEPA(
        metric=metric_with_feedback,
        auto=args.auto,
        reflection_lm=dspy.LM(model=reflection_model),
        track_stats=True,
    )
    optimized = optimizer.compile(program, trainset=trainset, valset=valset)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    optimized.save(str(args.out))
    print(f"wrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
