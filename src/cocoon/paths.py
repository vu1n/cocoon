"""Centralized cache-path resolution.

Getters are pure: they compute and return a path, no filesystem side effects.
Call `ensure_dirs()` once at process startup (the server and CLI both do).

The cache root is overridable via $COCOON_CACHE_DIR — useful for tests and
for users who want a non-default location.
"""

import os
from pathlib import Path


def cache_root() -> Path:
    override = os.environ.get("COCOON_CACHE_DIR")
    return Path(override) if override else Path.home() / ".cache" / "cocoon"


def auth_dir() -> Path:
    return cache_root() / "auth"


def catalog_dir() -> Path:
    return cache_root() / "catalog"


def agent_context_dir() -> Path:
    return cache_root() / "agent-context"


def binaries_dir() -> Path:
    """Cocoon-owned binary cache. Replaces v0.3's reliance on `$GOPATH/bin`
    so the host's PATH (which MCP subprocesses inherit from the daemon,
    not the user's shell) stops being a load-bearing dependency."""
    return cache_root() / "bin"


def press_auth_dir() -> Path:
    """Upstream printing-press CLIs store browser-derived (cookie/session)
    credentials here, encrypted, via their own `auth login`. Not under
    cocoon's cache root — it's the CLI's own state dir, by upstream
    convention — but cocoon must keep it out of the sandbox's read reach so
    one API's CLI can't read another's session store."""
    return Path.home() / ".press-auth"


def protected_credential_paths() -> tuple[Path, ...]:
    """Directories holding per-API secrets that a sandboxed CLI must never
    read: cocoon's own token files and the upstream press-auth store. Fed to
    SandboxPolicy.deny_read_paths so the cross-API isolation promise holds on
    the disk axis, not just the environment axis."""
    return (auth_dir(), press_auth_dir())


def ensure_dirs() -> None:
    for d in (auth_dir(), catalog_dir(), agent_context_dir(), binaries_dir()):
        d.mkdir(parents=True, exist_ok=True)
