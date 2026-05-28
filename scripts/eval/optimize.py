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

from dspy_discovery import build_program, pred_to_status_apis  # noqa: E402
from scoring import classify  # noqa: E402  — shared with run_discovery_eval.py

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


def metric_with_feedback(example, pred, trace=None, pred_name=None, pred_trace=None):
    """Thin adapter over scoring.classify. GEPA wants `Prediction(score, feedback)`;
    the runner wants the label — both come from one classification, parsed via
    the same `pred_to_status_apis` helper as `predict`."""
    import dspy
    status, apis = pred_to_status_apis(pred)
    outcome = classify(
        {"gold_api": example.gold_api, "gold_fall_through": example.gold_fall_through},
        status, apis,
    )
    return dspy.Prediction(score=1.0 if outcome.is_correct else 0.0,
                           feedback=outcome.feedback)


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
