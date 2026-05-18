"""Per-API credentials stored as JSON at ~/.cache/cocoon/auth/<api>.json.

File shape:

    {"env": {"LINEAR_TOKEN": "lin_abc123"}}

Tokens are read on demand by `call_capability` and passed into the
sandbox as env vars for that single invocation. They are never loaded
into the cocoon server's own environment, so a compromised CLI gets
only its own token, not the full set across APIs.
"""

import json
from pathlib import Path

from .errors import AuthMissing
from .paths import auth_dir


def _path_for(api: str) -> Path:
    return auth_dir() / f"{api}.json"


def load_token_env(api: str) -> dict[str, str]:
    """Return env vars for an API. Raises AuthMissing if no file exists."""
    path = _path_for(api)
    if not path.exists():
        raise AuthMissing(
            f"No auth configured for '{api}'.",
            api=api,
            path=str(path),
            setup_hint=f"cocoon auth {api} --token <token>",
        )

    data = json.loads(path.read_text(encoding="utf-8"))
    env = data.get("env", {})
    if not isinstance(env, dict):
        raise AuthMissing(
            f"Auth file for '{api}' has wrong shape (expected {{\"env\": {{...}}}}).",
            api=api,
            path=str(path),
        )
    return {str(k): str(v) for k, v in env.items()}


def write_token_env(api: str, env: dict[str, str]) -> str:
    """Write the auth file for an API. Returns the path written."""
    auth_dir().mkdir(parents=True, exist_ok=True)
    path = _path_for(api)
    path.write_text(json.dumps({"env": env}, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return str(path)
