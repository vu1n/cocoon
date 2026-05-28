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

    Sources the canonical product index — the same text cocoon's `find`
    attaches to fall_through responses — so the eval optimizes against
    the artifact that ships, not a separate copy."""
    from cocoon import catalog
    return catalog.compact_index()


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


# Defined at module scope (not inside build_program) so the signature class
# isn't re-declared per call. The class body needs dspy for InputField/
# OutputField, so we resolve it lazily — first call to _signature() configures
# the LM (idempotent) and returns the cached signature.
@cache
def _signature():
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

    return Discover


@cache
def build_program():
    """Construct the discovery dspy.Module — cached so the GEPA loop and the
    eval strategy share one program (and one LM context) across calls."""
    dspy = _configure_lm()
    Discover = _signature()

    class DiscoveryProgram(dspy.Module):
        def __init__(self, registry_text: str):
            super().__init__()
            self.registry_text = registry_text
            self.predict = dspy.Predict(Discover)

        def forward(self, query: str):
            pred = self.predict(query=query, registry=self.registry_text)
            return dspy.Prediction(api=(pred.api or "").strip())

    return DiscoveryProgram(registry_index())


def pred_to_status_apis(pred) -> tuple[str, set[str]]:
    """Translate a dspy.Prediction with `.api` into the eval's
    `(status, apis)` shape. Shared with optimize.metric_with_feedback so the
    pred-parsing lives in one place."""
    api = (getattr(pred, "api", "") or "").strip()
    return ("confident", {api}) if api else ("fall_through", set())


def predict(query: str) -> tuple[str, set[str]]:
    """Eval strategy entrypoint — routes one query via the (cached) dspy program."""
    return pred_to_status_apis(build_program()(query=query))
