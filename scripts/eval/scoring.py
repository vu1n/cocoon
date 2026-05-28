"""Canonical scoring for the discovery eval.

One classification of (example, prediction) → outcome + textual feedback. The
runner consumes the label; GEPA's metric (optimize.py) consumes both. Single
source of truth — keeps the per-example label and the optimizer's feedback in
lockstep so they cannot drift.
"""
from __future__ import annotations

from dataclasses import dataclass

ROUTED = "routed_correct"
MISROUTE = "misrouted"
BLUFF = "bluffed"
MISSED = "missed"
DECLINED = "declined"

_CORRECT = frozenset({ROUTED, DECLINED})


@dataclass(frozen=True)
class Outcome:
    label: str
    feedback: str

    @property
    def is_correct(self) -> bool:
        return self.label in _CORRECT


def gold_apis(example: dict) -> set[str]:
    """gold_api may be a single id or any iterable of ids — capability queries
    legitimately have several valid APIs (lasagna fits allrecipes OR food52).
    Accepts list/tuple/set so in-memory examples don't have to be lists."""
    g = example["gold_api"]
    if g is None:
        return set()
    if isinstance(g, (list, tuple, set, frozenset)):
        return set(g)
    return {g}


def _fmt(apis: set[str] | frozenset[str]) -> str:
    """repr for the feedback strings: scalar for a singleton, list otherwise.
    Avoids the `['stripe']` bracketed-singleton noise the GEPA reflector reads."""
    items = sorted(apis)
    return repr(items[0]) if len(items) == 1 else repr(items)


def classify(example: dict, status: str, apis) -> Outcome:
    """Score one prediction and emit actionable feedback. Used by the eval
    runner (label only) and GEPA's metric (label + feedback)."""
    apis = set(apis or ())  # accept any iterable; defensive vs list/None callers
    if example["gold_fall_through"]:
        if status == "fall_through":
            return Outcome(DECLINED, "Correct decline.")
        return Outcome(BLUFF, (
            f"BLUFF: routed to {_fmt(apis)}, but this query is out-of-corpus or "
            f"ordinary prose. A word matching an api id is not a route signal — "
            f"verify the api genuinely serves what the user is asking. Prefer "
            f"falling through (empty api)."
        ))
    gold = gold_apis(example)
    if status == "fall_through":
        return Outcome(MISSED, (
            f"MISSED: should have routed to one of {_fmt(gold)}. Re-read those "
            f"entries' descriptions and search_terms — the user's intent matches "
            f"that capability. Don't fall through when an api genuinely fits."
        ))
    if apis & gold:
        return Outcome(ROUTED, "Correct route.")
    return Outcome(MISROUTE, (
        f"MISROUTE: routed to {_fmt(apis)}, but the gold api(s) are {_fmt(gold)}. "
        f"The chosen api's description/search_terms do not fit this query. A "
        f"confidently-wrong route is worse than declining — when nothing clearly "
        f"fits, fall through."
    ))
