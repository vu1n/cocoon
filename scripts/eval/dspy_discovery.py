"""DSPy discovery strategy for the eval, and the program GEPA optimizes.

cocoon's runtime stays LM-free; this module is part of the `[optimize]` extra
and is invoked only by the eval (`--strategy dspy`) and the GEPA optimize
script. dspy is lazy-imported so the rest of the eval works without it.

Configure the LM via env:

    export DSPY_LM_MODEL=anthropic/claude-haiku-4-5     # any litellm model id
    export ANTHROPIC_API_KEY=...                        # provider key
    # optional: DSPY_REFLECTION_LM (defaults to DSPY_LM_MODEL)

The program is a single `dspy.Predict(Discover)` over a compact registry index
(one line per api). GEPA optimizes the signature's instructions + few-shots
against the eval metric — exactly the artifacts cocoon ships.
"""
from __future__ import annotations

import os
from functools import cache


@cache
def registry_index() -> str:
    """Compact one-line-per-api index, cached so a GEPA loop that calls
    predict() hundreds of times doesn't rebuild it per call.

    Shape: `api [category] — description | search_terms`
    """
    from cocoon import catalog
    lines: list[str] = []
    for c in catalog.list_categories():
        for s in catalog.list_apis(category=c.category):
            terms = ", ".join(list(s.search_terms)[:5])
            desc = (s.description or "")[:120]
            tail = f" | {terms}" if terms else ""
            lines.append(f"{s.api} [{s.category}] — {desc}{tail}")
    return "\n".join(lines)


def _configure_lm():
    """Lazy-import dspy + configure the LM from env. Raises a clear error when
    the extra isn't installed or DSPY_LM_MODEL isn't set."""
    try:
        import dspy
    except ImportError as exc:  # pragma: no cover — env-dependent
        raise RuntimeError(
            "dspy not installed. Install the optimize extra: "
            "`uv sync --extra optimize` (or `pip install cocoon-mcp[optimize]`)."
        ) from exc

    model = os.environ.get("DSPY_LM_MODEL")
    if not model:
        raise RuntimeError(
            "DSPY_LM_MODEL not set. Configure an LM via env, e.g.:\n"
            "  export DSPY_LM_MODEL=anthropic/claude-haiku-4-5\n"
            "  export ANTHROPIC_API_KEY=...   # or the provider's key"
        )
    if dspy.settings.lm is None:
        dspy.configure(lm=dspy.LM(model=model))
    return dspy


def build_program():
    """Construct the discovery dspy.Module. Importable + safe to call
    repeatedly; the LM is configured once."""
    dspy = _configure_lm()

    class Discover(dspy.Signature):
        """Route a user query to the best cocoon API, or fall through if none fit.

        Be strict on PRECISION: if the query is ordinary prose that merely
        contains a word matching an api id but isn't actually asking to use
        that service (e.g. "I need some clarity on this decision" is NOT a
        request for the clarity analytics API; "let your mind roam free" is
        NOT a request for the roam knowledge-base API), return an empty api
        (fall through).

        Be strict on CALIBRATION: when the user's described capability
        doesn't clearly match any api's description / search_terms, prefer
        falling through (empty api) over confidently picking a near-miss.
        Routing to the wrong api is worse than declining.
        """
        query: str = dspy.InputField()
        registry: str = dspy.InputField(
            desc="One line per api: `api [category] — description | search_terms`")
        api: str = dspy.OutputField(
            desc="The chosen api id (must be exactly one id from the registry), "
                 "or empty string to fall through. No prose.")

    class DiscoveryProgram(dspy.Module):
        def __init__(self, registry_text: str):
            super().__init__()
            self.registry_text = registry_text
            self.predict = dspy.Predict(Discover)

        def forward(self, query: str):
            pred = self.predict(query=query, registry=self.registry_text)
            return dspy.Prediction(api=(pred.api or "").strip())

    return DiscoveryProgram(registry_index())


_program_cache: list = []


def predict(query: str) -> tuple[str, set[str]]:
    """Eval strategy: route a query via the dspy program. Lazy-builds + caches
    the program so repeated calls reuse one LM context."""
    if not _program_cache:
        _program_cache.append(build_program())
    program = _program_cache[0]
    pred = program(query=query)
    api = pred.api
    if not api:
        return "fall_through", set()
    return "confident", {api}
