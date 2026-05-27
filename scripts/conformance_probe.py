"""Batch conformance probe over the cocoon catalog.

Materializes each catalogued CLI and runs `agent-context` under the tightening
ladder (see cocoon.conformance), then reports the tightest sandbox policy each
CLI tolerates. The output is the empirical input for tightening the seam:

  - `ready` @ synthetic_home → runs with a private ephemeral HOME; strongest tier
  - `ready` @ real_home      → needs its config/state from the real $HOME
  - `ready` @ network        → reaches network even to self-describe
  - `failed`                 → breaks under every policy; shim or upstream fix
  - `unavailable`            → no release asset to download (not a sandbox finding)
  - `skipped`                → no sandbox backend on this host

(A `ready` row whose probe_verb is `--help` lacks an `agent-context` subcommand,
so discovery enrichment is blind for it — see cocoon.conformance.)

This is a network- and disk-heavy audit (it downloads up to N binaries), so
it lives in scripts/ rather than `cocoon doctor`. Re-runs are fast: binaries
are cached under $COCOON_CACHE_DIR.

Usage:
    uv run python scripts/conformance_probe.py                 # whole catalog
    uv run python scripts/conformance_probe.py linear stripe   # named APIs only
    uv run python scripts/conformance_probe.py --limit 20 --jobs 6
    uv run python scripts/conformance_probe.py --out data/conformance.json
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import sys
import time
from collections import Counter
from pathlib import Path

# Allow running as a plain script (`python scripts/...`) without install.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cocoon import catalog as catalog_module  # noqa: E402
from cocoon import conformance  # noqa: E402

# Sort key for the report: most actionable first.
_STATUS_ORDER = {
    conformance.STATUS_FAILED: 0,
    conformance.STATUS_READY: 1,
    conformance.STATUS_UNAVAILABLE: 2,
    conformance.STATUS_SKIPPED: 3,
}
# Within ready, loosest-tier (most concerning) first.
_TIER_ORDER = {t.name: i for i, t in enumerate(reversed(conformance.TIERS))}

# ANSI, stripped when stdout isn't a TTY.
_COLOR = {
    conformance.STATUS_READY: "\033[32m",
    conformance.STATUS_FAILED: "\033[31m",
    conformance.STATUS_UNAVAILABLE: "\033[33m",
    conformance.STATUS_SKIPPED: "\033[90m",
}
_RESET = "\033[0m"


def _installable_apis() -> list[str]:
    """Catalog APIs cocoon can actually materialize (have an install_module).
    Entries without one can't be downloaded, so probing them is pointless."""
    return sorted(
        e["api"]
        for e in catalog_module.load_catalog()
        if e.get("api") and e.get("install_module")
    )


def _sort_key(o: conformance.ProbeOutcome) -> tuple:
    return (_STATUS_ORDER.get(o.status, 9), _TIER_ORDER.get(o.tier or "", -1), o.api)


def _paint(text: str, status: str, *, color: bool) -> str:
    if not color:
        return text
    return f"{_COLOR.get(status, '')}{text}{_RESET}"


def _print_report(outcomes: list[conformance.ProbeOutcome], *, color: bool) -> None:
    width = max((len(o.api) for o in outcomes), default=3)
    for o in sorted(outcomes, key=_sort_key):
        label = o.status if o.tier is None else f"{o.status}:{o.tier}"
        line = f"  {label:<18} {o.api:<{width}}  {o.finding}"
        print(_paint(line, o.status, color=color))


def _print_summary(outcomes: list[conformance.ProbeOutcome], elapsed: float) -> None:
    by_status = Counter(o.status for o in outcomes)
    ready_by_tier = Counter(o.tier for o in outcomes if o.status == conformance.STATUS_READY)
    print()
    print(f"probed {len(outcomes)} APIs in {elapsed:.1f}s")
    for status in (conformance.STATUS_READY, conformance.STATUS_FAILED,
                   conformance.STATUS_UNAVAILABLE, conformance.STATUS_SKIPPED):
        n = by_status.get(status, 0)
        if not n:
            continue
        print(f"  {status:<13} {n}")
        if status == conformance.STATUS_READY:
            for tier in conformance.TIERS:
                tn = ready_by_tier.get(tier.name, 0)
                if tn:
                    print(f"      @ {tier.name:<11} {tn}")


def _write_json(outcomes: list[conformance.ProbeOutcome], path: Path, elapsed: float) -> None:
    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "elapsed_s": round(elapsed, 1),
        "tiers": [t.name for t in conformance.TIERS],
        "summary": dict(Counter(o.status for o in outcomes)),
        "results": [o.to_dict() for o in sorted(outcomes, key=_sort_key)],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {path}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("apis", nargs="*", help="probe only these APIs (default: whole catalog)")
    parser.add_argument("--limit", type=int, default=0, help="probe at most N APIs")
    parser.add_argument("--jobs", type=int, default=4, help="parallel workers (default 4)")
    parser.add_argument("--out", type=Path, help="write the full JSON report to this path")
    parser.add_argument("--no-color", action="store_true", help="disable ANSI color")
    args = parser.parse_args(argv)

    apis = args.apis or _installable_apis()
    if args.limit > 0:
        apis = apis[: args.limit]
    if not apis:
        print("no installable APIs in catalog", file=sys.stderr)
        return 1

    color = (not args.no_color) and sys.stdout.isatty()
    print(f"probing {len(apis)} APIs with {args.jobs} workers ...", file=sys.stderr)

    start = time.monotonic()
    outcomes: list[conformance.ProbeOutcome] = []
    with cf.ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = {pool.submit(conformance.probe, api): api for api in apis}
        for fut in cf.as_completed(futures):
            api = futures[fut]
            try:
                outcomes.append(fut.result())
            except Exception as exc:  # defensive: one bad API shouldn't sink the run
                print(f"  ! {api}: probe raised {exc!r}", file=sys.stderr)
    elapsed = time.monotonic() - start

    _print_report(outcomes, color=color)
    _print_summary(outcomes, elapsed)
    if args.out:
        _write_json(outcomes, args.out, elapsed)

    # Exit non-zero if anything broke under every policy — useful as a CI gate
    # once the conformant baseline is established.
    failed = sum(1 for o in outcomes if o.status == conformance.STATUS_FAILED)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
