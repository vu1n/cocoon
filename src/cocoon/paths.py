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


def ensure_dirs() -> None:
    for d in (auth_dir(), catalog_dir()):
        d.mkdir(parents=True, exist_ok=True)
