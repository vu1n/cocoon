"""Per-API setup recipes: hand-curated guidance for getting the
credential cocoon needs (login URLs, cookie names, env vars). The
upstream printing-press manifests don't carry this metadata, so it
lives in `data/auth_recipes.json` and ships with the wheel.

Recipes flow out via `setup_recipe` on ApiSummary/Capability and
`setup_method` on auth_missing payloads. Tokens land via
`cocoon auth <api>`, which walks the user through the recipe
interactively when no flags are passed. Cocoon never plants
credentials through MCP — the boundary that no remote tool call
writes auth holds.
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
    return parsed["recipes"]


def recipe_for(api: str) -> dict[str, Any] | None:
    """The recipe for `api`, or None if cocoon doesn't ship one. None
    is the common case — only piloted APIs are covered. Treat None as
    'no automated guidance; user finds a token themselves'."""
    return load_recipes().get(api)
