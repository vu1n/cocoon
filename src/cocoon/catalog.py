"""Catalog: fetch from printing-press, parse, search.

The printing-press-library publishes a machine-readable manifest of
available APIs and their endpoints. The exact manifest URL is read at
call time from $COCOON_CATALOG_URL; when unset, cocoon falls back to a
small bundled dev catalog (5 APIs) so the server is exercisable before
the upstream manifest is finalized.

Cached on disk at ~/.cache/cocoon/catalog/index.json with a 24h TTL.
`refresh()` (and the `cocoon catalog refresh` CLI) force-evicts.

Search uses BM25 over a per-endpoint document = api + tool + summary +
flag names. That's enough relevance that the model usually picks the
right capability on the first round-trip; if not, `describe_capability`
gets it the rest of the way.
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


@dataclass(frozen=True)
class Capability:
    api: str
    tool: str
    summary: str
    params_schema: dict[str, Any]


@dataclass(frozen=True)
class ApiSummary:
    api: str
    description: str
    endpoint_count: int


def _catalog_url() -> str | None:
    """Read at call time so env changes take effect without re-import."""
    return os.environ.get("COCOON_CATALOG_URL")


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
    cached = _cache_path()
    if not refresh and _is_fresh(cached, CACHE_TTL_SECONDS):
        return json.loads(cached.read_text(encoding="utf-8"))

    url = _catalog_url()
    data = _fetch_remote(url) if url else _load_dev_catalog()

    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_text(json.dumps(data), encoding="utf-8")
    return data


def refresh_catalog() -> list[dict]:
    """Force a refresh and return the new catalog."""
    return load_catalog(refresh=True)


def _merged_view() -> list[dict]:
    """Combined catalog view: dev-catalog entries ∪ bundled-aggregate
    entries, with per-API agent-context endpoints spliced in.

    Three layers of priority for the endpoint list:
    1. Local agent-context cache — what's installed on this machine.
    2. Bundled aggregate — what upstream had at our last CI run.
    3. Dev catalog stub — hand-curated fallback for the 5 dev APIs.

    For non-endpoint fields (description, install_module) we use the
    union of dev-catalog + bundled-aggregate registry data; dev catalog
    wins on collision so tests that rely on the dev-catalog shape stay
    stable.
    """
    from . import agent_context  # lazy: avoid loading at module import

    entries_by_api: dict[str, dict] = {}

    # Bundled aggregate goes in first; dev catalog overlays.
    for bundled_entry in agent_context.bundled_apis():
        api = bundled_entry.get("name") or bundled_entry.get("api")
        if not api:
            continue
        mcp = bundled_entry.get("mcp") or {}
        path = bundled_entry.get("path", "")
        entries_by_api[api] = {
            "api": api,
            "description": bundled_entry.get("description", ""),
            "install_module": (
                f"github.com/mvanhorn/printing-press-library/{path}/cmd/{api}-pp-cli"
                if path else None
            ),
            "auth_type": mcp.get("auth_type") or "required",
            "endpoints": [],
        }

    for dev_entry in load_catalog():
        api = dev_entry.get("api")
        if api:
            entries_by_api[api] = dev_entry

    # Splice in real endpoint schemas: prefer the local cache, fall back
    # to the bundled aggregate's agent_context.
    out: list[dict] = []
    for api, entry in entries_by_api.items():
        ctx = agent_context.lookup(api)
        if ctx is None:
            out.append(entry)
            continue
        endpoints = [
            {"tool": cap["tool"],
             "summary": cap["summary"],
             "params_schema": cap["params_schema"]}
            for cap in agent_context.to_capabilities(api, ctx)
        ]
        out.append({**entry, "endpoints": endpoints})
    return out


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


def find_capability(query: str, limit: int = 5) -> list[Capability]:
    if not query.strip():
        return []

    entries: list[tuple[str, str, dict]] = []
    docs: list[str] = []
    for entry in _merged_view():
        api = entry["api"]
        api_desc = entry.get("description", "")
        for endpoint in entry.get("endpoints", []):
            entries.append((api, api_desc, endpoint))
            docs.append(_capability_doc(api, api_desc, endpoint))

    if not docs:
        return []

    scores = search.rank(query, docs)
    scored = [
        (score, Capability(
            api=api,
            tool=endpoint["tool"],
            summary=endpoint.get("summary", ""),
            params_schema=endpoint.get("params_schema", {}) or {},
        ))
        for score, (api, _desc, endpoint) in zip(scores, entries)
        if score > 0
    ]
    scored.sort(key=lambda pair: -pair[0])
    return [cap for _score, cap in scored[:limit]]


def describe_capability(api: str, tool: str) -> Capability:
    for entry in _merged_view():
        if entry["api"] != api:
            continue
        for endpoint in entry.get("endpoints", []):
            if endpoint["tool"] == tool:
                return Capability(
                    api=api,
                    tool=tool,
                    summary=endpoint.get("summary", ""),
                    params_schema=endpoint.get("params_schema", {}) or {},
                )
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


def list_apis(filter: str = "") -> list[ApiSummary]:
    needle = filter.lower().strip()
    out: list[ApiSummary] = []
    for entry in _merged_view():
        api = entry["api"]
        description = entry.get("description", "")
        if needle and needle not in api.lower() and needle not in description.lower():
            continue
        out.append(ApiSummary(
            api=api,
            description=description,
            endpoint_count=len(entry.get("endpoints", [])),
        ))
    return out


def to_dict(obj: Capability | ApiSummary) -> dict:
    return asdict(obj)
