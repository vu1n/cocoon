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


@dataclass(frozen=True)
class ApiSummary:
    api: str
    description: str
    endpoint_count: int
    auth_status: str = "required"
    category: str = "other"
    # Short curated discovery terms (aliases + capability slugs), surfaced so
    # the calling LLM can match by reasoning over upstream's curation.
    search_terms: tuple[str, ...] = ()


@dataclass(frozen=True)
class CategorySummary:
    category: str
    api_count: int


@dataclass(frozen=True)
class FindResult:
    """The reliable answer to "does cocoon have a tool for this?" — a tier-gate
    for an agent's capability-resolution ladder (have-a-skill → check cocoon →
    build it). `fall_through=True` is cocoon stating it has no confident match,
    so the caller should escalate (build the integration) rather than chase a
    weak lexical guess.

    Confidence comes from the query naming a service cocoon actually has, not
    from a BM25 score — calibration showed summary scores don't separate real
    matches from coincidental term overlap (a "slack" query out-ranked by
    `pushover`). When a service is named, `matches` are guaranteed to be that
    service's tools. When none is named, `fall_through` is set and any matches
    are clearly-advisory lexical guesses."""

    query: str
    matches: list[Capability]
    fall_through: bool
    reason: str


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
    registry's pre-flattened endpoints, plus auth_status derived from
    auth_type + presence of local credentials.

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
    """
    from . import agent_context  # lazy: avoid loading at module import

    out: list[dict] = []
    for entry in load_catalog():
        api = entry.get("api")
        if not api:
            continue
        auth_status = _derive_auth_status(api, entry.get("auth_type"))
        # Local cache wins if present; otherwise use the published endpoints.
        local = agent_context.cached(api)
        if local is None:
            out.append({**entry, "auth_status": auth_status})
            continue
        endpoints = [
            {"tool": cap["tool"],
             "summary": cap["summary"],
             "params_schema": cap["params_schema"],
             "positionals": cap.get("positionals", ()),
             "argv_path": cap.get("argv_path", ())}
            for cap in agent_context.to_capabilities(api, local)
        ]
        out.append({**entry, "endpoints": endpoints, "auth_status": auth_status})
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


# Search terms longer than this are full description sentences, not curated
# discovery keywords. Calibration showed folding the long ones into ranking
# lets an incidental word (a city in pointhound's description, "sfo") become a
# match signal — the postmortem's false-positive shape. So `find` ignores them;
# only the short, slug/label-like terms surface, and only in the browse index
# the LLM reads (not BM25 ranking).
_SEARCH_TERM_MAX_TOKENS = 4


def short_search_terms(search_terms: object) -> list[str]:
    """The short, slug/label-like curated terms for an API (aliases + capability
    names), dropping the long description sentences. Surfaced in the browse
    index so the calling LLM can reason over upstream's curated discovery
    keywords directly."""
    if not isinstance(search_terms, list):
        return []
    out: list[str] = []
    for term in search_terms:
        if not isinstance(term, str):
            continue
        # Gate on RAW word count (a slug is ≤4 words) so a stopword-heavy
        # fragment isn't mistaken for short once stopwords are dropped; require
        # a real token too, so punctuation-only junk is excluded.
        if 1 <= len(term.split()) <= _SEARCH_TERM_MAX_TOKENS and search.tokenize(term):
            out.append(term)
    return out


def find_capability(
    query: str,
    limit: int = 5,
    *,
    ready_only: bool = False,
    apis: set[str] | None = None,
    min_score: float | None = None,
) -> list[Capability]:
    """BM25-rank capabilities against `query`. Results sort ready APIs
    (auth_status none/configured) before gated ones (required), then by
    score descending within each band.

    `ready_only=True` filters gated APIs out entirely — useful when the
    agent only wants capabilities it can invoke without a user-attended
    setup step.

    `apis`, when given, restricts ranking to those API names — used by `find`
    to rank only within a service the query explicitly named (guaranteeing a
    right-service result instead of a cross-service lexical false positive).
    `min_score` overrides the default floor; pass a negative value to keep
    every endpoint of a named service even when the query barely lexically
    overlaps its summaries."""
    if not query.strip():
        return []

    rows: list[tuple[str, dict, str]] = []
    docs: list[str] = []
    for entry in _installable_view(ready_only=ready_only):
        api = entry["api"]
        if apis is not None and api not in apis:
            continue
        auth_status = entry.get("auth_status", "required")
        api_desc = entry.get("description", "")
        for endpoint in entry.get("endpoints", []):
            if _is_meta_tool(endpoint.get("tool", "")):
                continue  # agent-context/doctor/etc. aren't callable capabilities
            rows.append((api, endpoint, auth_status))
            docs.append(_capability_doc(api, api_desc, endpoint))

    if not docs:
        return []

    floor = _min_score() if min_score is None else min_score
    scores = search.rank(query, docs)
    scored = [
        (auth_status, score, _capability_from_endpoint(
            api, endpoint, score=score, auth_status=auth_status))
        for score, (api, endpoint, auth_status) in zip(scores, rows)
        if score > floor
    ]
    # Ready (none/configured) before required, then by score desc. Ties
    # within a readiness band preserve BM25 ordering.
    scored.sort(key=lambda triple: (_AUTH_RANK[triple[0]], -triple[1]))
    return [cap for _status, _score, cap in scored[:limit]]


# Cobra plumbing subcommands that show up in some agent-context dumps but
# aren't callable API operations. Filtered from find so a named service whose
# only catalog entry is the discover-on-install `agent-context` stub doesn't
# surface that stub as if it were a usable tool.
_META_TOOLS = frozenset(
    {"agent-context", "doctor", "completion", "help", "version", "__complete", "__completeNoDesc"})


def _is_meta_tool(tool: str) -> bool:
    # Cobra plumbing commands are bare ("agent-context", "completion bash");
    # real pp:endpoints are dotted ("version.game-list"). Match only the bare
    # form so a dotted endpoint that merely shares a prefix word (pokeapi's
    # `version.game-list`) isn't filtered out as if it were plumbing.
    if "." in tool:
        return False
    head = tool.split()
    return bool(head) and head[0] in _META_TOOLS


# Single-token API ids that are common English words. Matching them as a
# "named service" in ordinary prose ("I need clarity", "a little bird told me",
# "touring apartments", "pop music") produces false-confident routes — measured
# as bluffs in the discovery eval. They stay reachable via `list`/browse and
# keyword filter; only the confident find-gate skips them. This list is grown
# from measured eval bluffs, not guessed. The durable fix for name-vs-prose is
# the LLM discovery tier (which reasons about intent); this is a bounded guard.
_AMBIGUOUS_API_IDS = frozenset({"clarity", "bird", "apartments", "pop"})


def _named_apis(query: str) -> list[str]:
    """API names the query explicitly mentions. An API matches when either its
    de-spaced name is a query token or adjacent-token join ("hacker news" →
    `hackernews`), or every token of its hyphen-split name appears in the query
    ("alaska airlines flights" → `alaska-airlines`). A bare "airlines" matches
    neither (too generic to claim). This service-name signal is cocoon's
    high-precision gate; summary BM25 is not (see FindResult)."""
    tokens = search.tokenize(query)
    if not tokens:
        return []
    token_set = set(tokens)
    # tokens + adjacent concatenations, so a service written as two words still
    # matches a single-token API id.
    forms = token_set | {tokens[i] + tokens[i + 1] for i in range(len(tokens) - 1)}
    named: list[str] = []
    for entry in _installable_view():
        api = entry.get("api")
        if not api or api in _AMBIGUOUS_API_IDS:
            continue
        parts = [p for p in search.tokenize(api.replace("-", " ")) if p]
        if not parts:
            continue
        despaced = "".join(parts)
        if despaced in forms or (len(parts) > 1 and all(p in token_set for p in parts)):
            named.append(api)
    return named


def find(query: str, limit: int = 5, *, ready_only: bool = False) -> FindResult:
    """Reliable tier-gate over the catalog. If the query names a service cocoon
    has, return that service's closest tools (confident, no fall-through). If
    not, return a fall-through verdict with any lexical guesses marked advisory,
    so an agent escalates to building rather than chasing a false positive."""
    if not query.strip():
        return FindResult(query, [], fall_through=True, reason="empty query")

    named = _named_apis(query)
    if named:
        # Rank only within the named service(s); negative floor keeps the
        # service's tools even when the query barely overlaps their summaries
        # (the user named it — show what it can do).
        matches = find_capability(
            query, limit, ready_only=ready_only, apis=set(named), min_score=float("-inf"))
        joined = ", ".join(sorted(named))
        if matches:
            return FindResult(
                query, matches, fall_through=False,
                reason=f"cocoon has {joined}; closest tool(s) below")
        # Named, so cocoon HAS the service — don't tell the agent to rebuild it
        # — but no callable tool is listed: either it's a manifest-less CLI
        # whose tools resolve on first install, or ready_only filtered its
        # (auth-gated) tools out. Either way the next step is describe/call, not
        # build, so this is not a fall-through.
        return FindResult(
            query, [], fall_through=False,
            reason=f"cocoon has {joined}, but its tools aren't listed here "
                   f"(resolved on first call, or hidden by ready_only); "
                   f"call or describe to populate them")

    # No service named — summary BM25 alone is unreliable, so this is advisory.
    guesses = find_capability(query, limit, ready_only=ready_only)
    return FindResult(
        query, guesses, fall_through=True,
        reason=("no service in your query matches one cocoon has; the entries "
                "below are unverified lexical guesses — verify they fit your "
                "intent or build the integration"))


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
    )


def describe_capability(api: str, tool: str) -> Capability:
    for entry in _merged_view():
        if entry["api"] != api:
            continue
        auth_status = entry.get("auth_status", "required")
        for endpoint in entry.get("endpoints", []):
            if endpoint["tool"] == tool:
                return _capability_from_endpoint(
                    api, endpoint, auth_status=auth_status)
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


def list_categories(*, ready_only: bool = False) -> list[CategorySummary]:
    """The browse menu: each category cocoon has and its API count. This is the
    cheap top level (~tens of lines) so the calling LLM can pick a category
    before pulling that category's larger API index."""
    counts: dict[str, int] = {}
    for entry in _installable_view(ready_only=ready_only):
        cat = entry.get("category") or "other"
        counts[cat] = counts.get(cat, 0) + 1
    return [CategorySummary(category=c, api_count=n) for c, n in sorted(counts.items())]


def list_apis(
    filter: str = "", category: str = "", *, ready_only: bool = False
) -> list[ApiSummary]:
    """A compact, scannable API index for the calling LLM to search itself.
    Narrow with `category` (exact) and/or `filter` (keyword over name +
    description + curated search_terms). Each entry carries its category and
    short curated terms so the LLM can match by reasoning rather than relying on
    cocoon's ranking. Sorts ready (none/configured) before gated."""
    needle = filter.lower().strip()
    cat_needle = category.lower().strip()
    out: list[ApiSummary] = []
    for entry in _installable_view(ready_only=ready_only):
        cat = entry.get("category") or "other"
        if cat_needle and cat.lower() != cat_needle:
            continue
        api = entry["api"]
        description = entry.get("description", "")
        terms = short_search_terms(entry.get("search_terms"))
        if needle and needle not in " ".join([api, description, *terms]).lower():
            continue
        # Count callable endpoints only; a manifest-less CLI whose sole entry is
        # the agent-context stub should read as 0, not 1 (its tools resolve on
        # first install) — see _is_meta_tool.
        callable_count = sum(
            1 for e in entry.get("endpoints", []) if not _is_meta_tool(e.get("tool", "")))
        out.append(ApiSummary(
            api=api,
            description=description,
            endpoint_count=callable_count,
            auth_status=entry.get("auth_status", "required"),
            category=cat,
            search_terms=tuple(terms),
        ))
    out.sort(key=lambda s: (_AUTH_RANK[s.auth_status], s.api))
    return out


def to_dict(obj: Capability | ApiSummary | CategorySummary | FindResult) -> dict:
    """Serialize any catalog result dataclass. asdict recurses, so a FindResult
    serializes its Capability matches too — no bespoke serializer needed."""
    return asdict(obj)
