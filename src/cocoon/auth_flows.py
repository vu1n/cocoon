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

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

from . import materialize
from .paths import press_auth_dir

# A press-auth filename is `<domain>.json` — domains and the extension are
# alphanumerics, dots, dashes. Anything else (path separators, quotes,
# whitespace, control chars) is rejected before the name reaches the sandbox
# profile: it could break out of the SBPL `(subpath "...")` string literal or
# widen the read scope beyond ~/.press-auth.
_SAFE_PRESS_AUTH_NAME = re.compile(r"^[A-Za-z0-9._-]+$")

# Auth types where cocoon defers to the CLI's own auth login subcommand.
# These all involve browser-derived state that the CLI's encrypted
# ~/.press-auth store is purpose-built for.
_DELEGATED_AUTH_TYPES = {"cookie", "session_handshake", "composed"}

# Marker key holding the JSON list of ~/.press-auth filenames this API's login
# produced. Lets the call-time sandbox expose only this API's session store
# (not every delegated API's). Stored in the marker env — never injected into
# the sandbox (the delegated call path drops the marker env).
_PRESS_AUTH_FILES_KEY = "_PRESS_AUTH_FILES"

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
    # Snapshot ~/.press-auth around the login so we can attribute the file(s)
    # it writes to THIS api — the store is keyed by domain, not api, and the
    # domain isn't exposed in metadata, so observing the write is the only
    # CLI-agnostic way to learn which file is this api's.
    before = _press_auth_snapshot()
    result = subprocess.run([str(binary), "auth", "login", "--chrome"])
    if result.returncode != 0:
        print(f"error: `{binary.name} auth login` exited {result.returncode}",
              file=sys.stderr)
        return None
    # Stringify the marker so write_token_env's env-var contract holds.
    marker = {f"_{k.upper()}": str(v) for k, v in _DELEGATED_MARKER.items()}
    marker[_PRESS_AUTH_FILES_KEY] = json.dumps(_changed_press_auth_files(before))
    return marker


def _press_auth_snapshot() -> dict[str, str]:
    """filename -> content hash for files directly under ~/.press-auth. We
    hash contents rather than (mtime, size) so an in-place rewrite to an
    identical size (cookie blobs are often fixed-length) is still detected —
    a missed change would silently fall back to exposing the whole store."""
    base = press_auth_dir()
    if not base.exists():
        return {}
    snap: dict[str, str] = {}
    for p in base.iterdir():
        if p.is_file():
            try:
                snap[p.name] = hashlib.sha256(p.read_bytes()).hexdigest()
            except OSError:
                continue
    return snap


def _changed_press_auth_files(before: dict[str, str]) -> list[str]:
    """Files created or with changed contents since `before`. Empty when the
    login rewrote nothing (e.g. already-valid creds) — the call path then
    falls back to exposing the whole store rather than guessing.

    Note: this attributes whatever the login window touched to this api. A
    concurrent writer in ~/.press-auth would be over-claimed; logins are
    interactive (Chrome) and expected to run one at a time."""
    after = _press_auth_snapshot()
    return sorted(name for name, h in after.items() if before.get(name) != h)


def press_auth_paths(marker_env: dict[str, str]) -> tuple[Path, ...]:
    """The specific ~/.press-auth file(s) a delegated API owns, as recorded in
    its marker at login time. Empty when unknown (pre-scoping marker, or the
    login rewrote nothing) — the caller falls back to the whole store.

    Filenames come from a dir listing (basenames), but we re-validate here
    against a strict charset before turning them into paths the sandbox will
    expose: a name with a separator, quote, or control char could escape
    ~/.press-auth or break out of the SBPL `(subpath "...")` literal."""
    raw = marker_env.get(_PRESS_AUTH_FILES_KEY)
    if not raw:
        return ()
    try:
        names = json.loads(raw)
    except (ValueError, TypeError):
        return ()
    if not isinstance(names, list):
        return ()
    base = press_auth_dir()
    return tuple(
        base / n
        for n in names
        if isinstance(n, str) and n not in (".", "..") and _SAFE_PRESS_AUTH_NAME.match(n)
    )


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
