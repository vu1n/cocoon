"""Per-API setup recipes: hand-curated guidance for getting the
credential cocoon needs (login URLs, cookie names, env vars). The
upstream printing-press manifests don't carry this metadata, so it
lives in `data/auth_recipes.json` and ships with the wheel.

Cocoon stays read-only on tokens via MCP: `setup_auth` surfaces the
recipe; the user runs `cocoon setup-auth` or `cocoon auth` to write
the token. Keeps the boundary that no remote tool call plants
credentials. Underscore-prefixed top-level keys (e.g. `_comment`)
are stripped on load.
"""

import importlib.resources
import json
from functools import cache
from typing import Any


@cache
def load_recipes() -> dict[str, dict[str, Any]]:
    """Bundled recipe table, keyed by API name. Cached process-wide
    — recipes are static, embedded in the wheel."""
    data = importlib.resources.files(__package__).joinpath("data/auth_recipes.json")
    parsed = json.loads(data.read_text(encoding="utf-8"))
    return {k: v for k, v in parsed.items() if not k.startswith("_")}


def recipe_for(api: str) -> dict[str, Any] | None:
    """The recipe for `api`, or None if cocoon doesn't ship one. None
    is the common case — only piloted APIs are covered. Treat None as
    'no automated guidance; user finds a token themselves'."""
    return load_recipes().get(api)
