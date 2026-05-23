"""Catalog: fetch the cocoon-published registry, parse, search.

The cocoon repo runs a nightly GitHub Action that harvests
printing-press-library's per-CLI manifests and publishes the aggregated
result as a single JSON file. cocoon's runtime fetches that file on
first need (URL configurable via $COCOON_CATALOG_URL; default points at
raw.githubusercontent.com of cocoon's main branch) and caches it locally
with a 24h TTL. Single source of truth, no wheel-bundled drift.

When the network is unreachable on first run, cocoon falls back to a
small bundled dev catalog (5 APIs) so the CLI is still exercisable.

The published registry is a list of dicts, each carrying api name,
description, install_module, auth_type, and pre-flattened endpoints
with positionals + argv_path. The local agent-context cache (captured
post-install from each installed binary) overrides any entry's
endpoints — that's the most authoritative view for installed APIs.

Search uses BM25 over per-endpoint docs = api + description + tool +
summary + flag names. Stopwords filtered for query quality.
"""

import importlib.resources
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx

from . import search
from .errors import CapabilityNotFound, CatalogUnavailable
from .paths import catalog_dir

CACHE_FILE = "index.json"
CACHE_TTL_SECONDS = 24 * 60 * 60

# Default published registry URL. Overridable via $COCOON_CATALOG_URL.
# raw.githubusercontent.com is fine for the scale we're at; if rate
# limits bite or we want better caching control, switch to GH Pages.
DEFAULT_REGISTRY_URL = "https://raw.githubusercontent.com/vu1n/cocoon/main/data/registry.json"


# Readiness buckets for sort ordering. "none" and "configured" share a
# bucket — both are callable now; "required" needs a setup step first.
# Unknown statuses fall to the back via the default in _AUTH_RANK.get.
_AUTH_RANK: dict[str, int] = {"none": 0, "configured": 0, "required": 1}


@dataclass(frozen=True)
class Capability:
    api: str
    tool: str
    summary: str
    params_schema: dict[str, Any]
    # Names of cobra-style positional args in declared order. Used by
    # tool_argv to emit `items 12345` instead of `items --itemId=12345`
    # for commands like `items <itemId>`.
    positionals: tuple[str, ...] = ()
    # Actual cobra subcommand chain to invoke this capability — derived
    # from where the `pp:endpoint` annotation was found in the command
    # tree. Distinct from `tool` because pp:endpoint names can carry verb
    # suffixes (e.g. `items.get`) that don't correspond to real
    # subcommands (the cobra invocation is just `items <itemId>`).
    argv_path: tuple[str, ...] = ()
    # BM25 score from find_capability. 0.0 for capabilities returned via
    # describe or list (where ranking didn't apply). Surfaced so agents
    # that reason about confidence can ignore weak matches even when
    # COCOON_FIND_MIN_SCORE isn't set.
    score: float = 0.0
    # Readiness: "none" (no auth needed) | "configured" (auth needed and
    # token file present) | "required" (auth needed, no token yet — agent
    # should surface a setup step instead of attempting the call).
    auth_status: str = "required"
    # The per-API setup recipe when cocoon ships one — login URL, env
    # var, instructions. None for the vast majority of APIs. Agents
    # surface this to the user verbatim instead of guessing.
    setup_recipe: dict[str, Any] | None = None


@dataclass(frozen=True)
class ApiSummary:
    api: str
    description: str
    endpoint_count: int
    auth_status: str = "required"
    setup_recipe: dict[str, Any] | None = None


def _catalog_url() -> str:
    """Read at call time so env changes take effect without re-import.
    Defaults to the cocoon-published registry; users can pin a local file
    or a fork via the env var."""
    return os.environ.get("COCOON_CATALOG_URL") or DEFAULT_REGISTRY_URL


def _cache_path() -> Path:
    return catalog_dir() / CACHE_FILE


def _load_dev_catalog() -> list[dict]:
    data = importlib.resources.files(__package__).joinpath("data/dev_catalog.json")
    return json.loads(data.read_text(encoding="utf-8"))


def _fetch_remote(url: str) -> list[dict]:
    try:
        response = httpx.get(url, timeout=30, follow_redirects=True)
        response.raise_for_status()
        return response.json()
    except httpx.HTTPError as exc:
        raise CatalogUnavailable(
            f"Failed to fetch catalog from {url}: {exc}",
            url=url,
        ) from exc


def _is_fresh(path: Path, ttl: int) -> bool:
    if not path.exists():
        return False
    age = time.time() - path.stat().st_mtime
    return age < ttl


def load_catalog(*, refresh: bool = False) -> list[dict]:
    """Return the canonical catalog list. On cache miss / expiry, fetches
    the published registry. If the network is unreachable, falls back to
    the bundled dev catalog (5 APIs) so the CLI still works offline."""
    cached = _cache_path()
    if not refresh and _is_fresh(cached, CACHE_TTL_SECONDS):
        return json.loads(cached.read_text(encoding="utf-8"))

    url = _catalog_url()
    try:
        data = _fetch_remote(url)
    except CatalogUnavailable:
        # Offline / first-run-with-no-network. Keep working against the
        # 5-API dev catalog so users hit a useful "find" rather than a
        # crash. NOT cached — we want the next attempt to re-try the
        # network, not be stuck on this fallback for 24h.
        return _load_dev_catalog()

    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_text(json.dumps(data), encoding="utf-8")
    return data


def refresh_catalog() -> list[dict]:
    """Force a refresh and return the new catalog."""
    return load_catalog(refresh=True)


def _merged_view() -> list[dict]:
    """Catalog with local agent-context cache spliced over the published
    registry's pre-flattened endpoints, plus auth_status + setup_recipe
    derived from auth_type + presence of local credentials + bundled
    recipe data.

    Two layers of priority for the endpoint list:
    1. Local agent-context cache — what's installed on this machine. The
       most authoritative view for any API the user has actually called.
    2. Published catalog endpoints — pre-flattened by the build script
       from upstream tools-manifests + synthetic stubs.

    Non-endpoint fields (api, description, install_module, auth_type)
    come from the catalog entry as-is. `auth_status` is derived per-entry:
      "none"       → auth_type=="none" (callable immediately)
      "configured" → auth_type required AND ~/.cache/cocoon/auth/<api>.json exists
      "required"   → auth needed but not yet configured (deferred until setup)
    `setup_recipe` is the bundled per-API setup recipe (login URL, env
    var, instructions) when one ships, else None.
    """
    from . import agent_context  # lazy: avoid loading at module import
    from . import auth_recipes

    out: list[dict] = []
    for entry in load_catalog():
        api = entry.get("api")
        if not api:
            continue
        auth_status = _derive_auth_status(api, entry.get("auth_type"))
        recipe = auth_recipes.recipe_for(api)
        # Local cache wins if present; otherwise use the published endpoints.
        local = agent_context.cached(api)
        if local is None:
            out.append({**entry, "auth_status": auth_status, "setup_recipe": recipe})
            continue
        endpoints = [
            {"tool": cap["tool"],
             "summary": cap["summary"],
             "params_schema": cap["params_schema"],
             "positionals": cap.get("positionals", ()),
             "argv_path": cap.get("argv_path", ())}
            for cap in agent_context.to_capabilities(api, local)
        ]
        out.append({**entry, "endpoints": endpoints,
                    "auth_status": auth_status, "setup_recipe": recipe})
    return out


def _derive_auth_status(api: str, auth_type: str | None) -> str:
    """Compute auth_status from auth_type + presence of a local token.
    One Path.exists per call via auth.is_configured; does NOT read the
    token file content."""
    from . import auth
    if not auth_type or auth_type == "none":
        return "none"
    return "configured" if auth.is_configured(api) else "required"


def _installable_view(*, ready_only: bool = False) -> list[dict]:
    """`_merged_view` filtered to entries cocoon could actually invoke,
    optionally also dropping auth-gated entries.

    An entry without `install_module` would fail at materialize-time
    with `materialization_failed`. Surfacing such an entry from
    `find`/`list` invites the agent to try the call and waste a
    round-trip. The filter prevents that.

    `describe_capability` deliberately uses `_merged_view` (no filter):
    if the agent already has the name, inspection should succeed.
    """
    return [
        e for e in _merged_view()
        if e.get("install_module")
        and not (ready_only and e.get("auth_status") == "required")
    ]


def installable_skip_count() -> int:
    """How many catalog entries are uncallable due to missing install_module.
    Exposed for `cocoon doctor` so the gap is visible at health-check time."""
    return sum(1 for e in _merged_view() if not e.get("install_module"))


def _capability_doc(api: str, api_desc: str, endpoint: dict) -> str:
    """Render the searchable text for one endpoint."""
    parts = [
        api,
        api_desc,
        endpoint.get("tool", ""),
        endpoint.get("summary", ""),
        *(endpoint.get("params_schema", {}) or {}).keys(),
    ]
    return " ".join(parts)


def find_capability(query: str, limit: int = 5, *, ready_only: bool = False) -> list[Capability]:
    """BM25-rank capabilities against `query`. Results sort ready APIs
    (auth_status none/configured) before gated ones (required), then by
    score descending within each band.

    `ready_only=True` filters gated APIs out entirely — useful when the
    agent only wants capabilities it can invoke without a user-attended
    setup step."""
    if not query.strip():
        return []

    rows: list[tuple[str, dict, str, dict | None]] = []
    docs: list[str] = []
    for entry in _installable_view(ready_only=ready_only):
        api = entry["api"]
        auth_status = entry.get("auth_status", "required")
        recipe = entry.get("setup_recipe")
        api_desc = entry.get("description", "")
        for endpoint in entry.get("endpoints", []):
            rows.append((api, endpoint, auth_status, recipe))
            docs.append(_capability_doc(api, api_desc, endpoint))

    if not docs:
        return []

    floor = _min_score()
    scores = search.rank(query, docs)
    scored = [
        (auth_status, score, _capability_from_endpoint(
            api, endpoint, score=score,
            auth_status=auth_status, setup_recipe=recipe))
        for score, (api, endpoint, auth_status, recipe) in zip(scores, rows)
        if score > floor
    ]
    # Ready (none/configured) before required, then by score desc. Ties
    # within a readiness band preserve BM25 ordering.
    scored.sort(key=lambda triple: (_AUTH_RANK[triple[0]], -triple[1]))
    return [cap for _status, _score, cap in scored[:limit]]


_warned_min_score_values: set[str] = set()


def _min_score() -> float:
    """Floor below which find_capability drops a match. Defaults to 0
    (return any positive-score match). Set $COCOON_FIND_MIN_SCORE to a
    higher threshold once real query logs let you calibrate — the
    postmortem identified BM25 false-positives (e.g. `pointhound` for
    'commercial flights') as a fan-out trigger, and a floor cuts them.

    Bad values log a warning to stderr (once per distinct bad value, so
    the long-lived MCP server doesn't spam every find call) and fall back
    to 0.0 rather than silently no-op'ing — a user who typo'd the env var
    would otherwise see no filtering and assume the knob doesn't work."""
    raw = os.environ.get("COCOON_FIND_MIN_SCORE")
    if not raw:
        return 0.0
    try:
        return float(raw)
    except ValueError:
        if raw not in _warned_min_score_values:
            _warned_min_score_values.add(raw)
            import sys
            print(
                f"warning: COCOON_FIND_MIN_SCORE={raw!r} is not a number; "
                f"falling back to 0.0 (no threshold).",
                file=sys.stderr,
            )
        return 0.0


def _capability_from_endpoint(
    api: str,
    endpoint: dict,
    *,
    score: float = 0.0,
    auth_status: str = "required",
    setup_recipe: dict | None = None,
) -> Capability:
    return Capability(
        api=api,
        tool=endpoint["tool"],
        summary=endpoint.get("summary", ""),
        params_schema=endpoint.get("params_schema", {}) or {},
        positionals=tuple(endpoint.get("positionals", ())),
        argv_path=tuple(endpoint.get("argv_path", ())),
        score=score,
        auth_status=auth_status,
        setup_recipe=setup_recipe,
    )


def describe_capability(api: str, tool: str) -> Capability:
    for entry in _merged_view():
        if entry["api"] != api:
            continue
        auth_status = entry.get("auth_status", "required")
        recipe = entry.get("setup_recipe")
        for endpoint in entry.get("endpoints", []):
            if endpoint["tool"] == tool:
                return _capability_from_endpoint(
                    api, endpoint, auth_status=auth_status, setup_recipe=recipe)
    raise CapabilityNotFound(
        f"No capability '{tool}' found for api '{api}'",
        api=api,
        tool=tool,
    )


def _entry_for(api: str) -> dict | None:
    for entry in _merged_view():
        if entry.get("api") == api:
            return entry
    return None


def auth_type(api: str) -> str:
    """Return the auth_type field for an api, defaulting to 'required'.

    Prefers the locally-installed CLI's own agent-context `.auth.mode` if
    present (most authoritative — it's what the binary will actually do),
    falling back to the dev/upstream catalog's `auth_type` field, and
    finally to `"required"` (the safe default that forces an explicit auth
    file rather than silently passing an empty env into the sandbox).
    """
    from . import agent_context
    mode = agent_context.auth_mode(agent_context.cached(api))
    if mode is not None:
        return mode
    entry = _entry_for(api)
    return entry.get("auth_type", "required") if entry else "required"


def install_module(api: str) -> str | None:
    """Return the Go module path to install for an api, or None if absent."""
    entry = _entry_for(api)
    module = entry.get("install_module") if entry else None
    return module if isinstance(module, str) else None


def list_apis(filter: str = "", *, ready_only: bool = False) -> list[ApiSummary]:
    """List APIs in the catalog. Results sort ready (none/configured)
    before required so the agent sees what's immediately callable first.
    `ready_only=True` filters gated APIs out entirely."""
    needle = filter.lower().strip()
    out: list[ApiSummary] = []
    for entry in _installable_view(ready_only=ready_only):
        api = entry["api"]
        description = entry.get("description", "")
        if needle and needle not in api.lower() and needle not in description.lower():
            continue
        out.append(ApiSummary(
            api=api,
            description=description,
            endpoint_count=len(entry.get("endpoints", [])),
            auth_status=entry.get("auth_status", "required"),
            setup_recipe=entry.get("setup_recipe"),
        ))
    out.sort(key=lambda s: (_AUTH_RANK[s.auth_status], s.api))
    return out


def to_dict(obj: Capability | ApiSummary) -> dict:
    return asdict(obj)
