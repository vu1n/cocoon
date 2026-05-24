"""Auth-setup orchestration. Cocoon delegates to the per-API CLI's
own auth subcommand instead of reimplementing credential acquisition.

Why delegation, not implementation:
- The per-CLI `<api>-pp-cli auth login --chrome` already exists upstream,
  uses pycookiecheat/cookie-scoop, supports multiple Chrome profiles,
  and encrypts cookies at rest under ~/.press-auth via macOS keychain.
- Anything cocoon would build here would be a strict downgrade or
  duplicate. The right boundary: the CLI owns its credential lifecycle;
  cocoon orchestrates discovery + execution + sandbox around it.

Dispatch by auth_type:
- `cookie` / `session_handshake` / `composed` → delegate `auth login
  --chrome` to the CLI. The CLI writes encrypted state to
  ~/.press-auth/<domain>.json; cocoon writes a marker file so
  auth_status flips to "configured" for catalog display.
- `api_key` / `bearer_token` / other → cocoon-owned secret via
  `token_paste_flow`. Cocoon writes ~/.cache/cocoon/auth/<api>.json
  with the env var; passes it into the per-call sandbox. The CLI sees
  the env var without ever touching cocoon's filesystem.
- `none` → user error; the API doesn't need auth.
"""

import subprocess
import sys

from . import materialize

# Auth types where cocoon defers to the CLI's own auth login subcommand.
# These all involve browser-derived state that the CLI's encrypted
# ~/.press-auth store is purpose-built for.
_DELEGATED_AUTH_TYPES = {"cookie", "session_handshake", "composed"}

# Sentinel written to cocoon's auth file for delegated APIs. Presence
# of the file flips auth_status to "configured"; the body documents
# where the real credentials live so a curious user (or `cocoon doctor`)
# can find them.
_DELEGATED_MARKER = {
    "delegated_to": "press-auth",
    "note": "Credentials managed by the upstream CLI under ~/.press-auth/. "
            "Run `<api>-pp-cli auth status` for live state.",
}


def is_delegated(auth_type: str) -> bool:
    """Whether cookie/session auth_types defer to the CLI's own login."""
    return auth_type in _DELEGATED_AUTH_TYPES


def run(api: str, auth_type: str) -> dict[str, str] | None:
    """Dispatch to the right setup flow. Returns the env dict to
    persist in cocoon's auth file, or None on abort/error.

    For delegated auth_types, returns the marker dict — the real
    credentials live with the upstream CLI."""
    if auth_type == "none":
        print(f"error: '{api}' doesn't require auth", file=sys.stderr)
        return None
    if is_delegated(auth_type):
        return delegate_login(api)
    return token_paste_flow(api, auth_type)


def delegate_login(api: str) -> dict[str, str] | None:
    """Materialize the CLI and exec its `auth login --chrome`. The
    subprocess inherits the parent's stdio so the CLI can drive
    interactive flows (Chrome opening, multi-profile selection)
    without cocoon mediating."""
    try:
        binary = materialize.materialize(api)
    except Exception as exc:
        print(f"error: couldn't materialize {api} CLI: {exc}", file=sys.stderr)
        return None

    print(f"Running `{binary.name} auth login --chrome` ...")
    print()
    result = subprocess.run([str(binary), "auth", "login", "--chrome"])
    if result.returncode != 0:
        print(f"error: `{binary.name} auth login` exited {result.returncode}",
              file=sys.stderr)
        return None
    # Stringify the marker so write_token_env's env-var contract holds.
    return {f"_{k.upper()}": str(v) for k, v in _DELEGATED_MARKER.items()}


def token_paste_flow(api: str, auth_type: str) -> dict[str, str] | None:
    """Prompt for a single token value. Cocoon owns the secret —
    written to ~/.cache/cocoon/auth/<api>.json, passed into the
    per-call sandbox as <API>_TOKEN. The CLI reads the env var; no
    file mount required."""
    env_var = _env_var_for(api, suffix="TOKEN")
    print(f"Setup for `{api}` ({auth_type}):")
    print(f"  Paste your {auth_type} value below. It'll be saved as {env_var}.")
    try:
        value = input("> ").strip()
    except EOFError:
        value = ""
    if not value:
        print("error: empty input; not writing", file=sys.stderr)
        return None
    return {env_var: value}


def _env_var_for(api: str, *, suffix: str) -> str:
    """Conventional env var: <API>_<SUFFIX> with hyphens turned into
    underscores. Only used by token_paste_flow — delegated auth types
    don't pass env vars (the CLI reads its own state file)."""
    return f"{api.upper().replace('-', '_')}_{suffix}"
