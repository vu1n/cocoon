"""Seamless install + lookup of printing-press CLIs.

The agent never has to take a separate install step. When a capability is
called and its binary (`<api>-pp-cli`) isn't on PATH, cocoon runs
`go install <module>@latest` for the module declared in the catalog
entry's `install_module` field. After install the binary lives at
`$GOPATH/bin/<api>-pp-cli` (typically `~/go/bin`); cocoon extends PATH
internally so callers don't need to remember this.

The install runs unsandboxed in v0 — `go install` needs network access
plus write to $GOPATH. The curated catalog is the trust boundary for
which modules can be installed. v1.1 will sandbox the install step too.
Per-call execution (the load-bearing security boundary) is always
sandboxed.
"""

import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable

from . import catalog as catalog_module
from .errors import MaterializationFailed

ProgressCallback = Callable[[str], None]


def path_with_gobin() -> str:
    """PATH extended with $HOME/go/bin (where `go install` drops binaries)."""
    go_bin = str(Path.home() / "go" / "bin")
    current = os.environ.get("PATH", "")
    parts = current.split(os.pathsep)
    return current if go_bin in parts else os.pathsep.join([*parts, go_bin])


def _binary_name(api: str) -> str:
    return f"{api}-pp-cli"


def cached_binary(api: str) -> Path | None:
    found = shutil.which(_binary_name(api), path=path_with_gobin())
    return Path(found) if found else None


def materialize(api: str, *, on_progress: ProgressCallback | None = None) -> Path:
    existing = cached_binary(api)
    if existing:
        # Binary already on PATH (cached or installed manually). Make sure
        # we still have its agent-context dump for catalog enrichment —
        # users who installed before cocoon, or first-time cocoon users
        # whose go cache is warm, would otherwise never get the schema.
        _ensure_agent_context(existing, api)
        return existing

    searched = path_with_gobin()
    go = shutil.which("go", path=searched)
    if go is None:
        raise MaterializationFailed(
            f"Go toolchain not on PATH; cannot install '{_binary_name(api)}'. "
            "Either (a) Go isn't installed — get it from https://go.dev/dl/, OR "
            "(b) Go IS installed but the MCP host subprocess inherited a PATH "
            "that excludes it (host daemons don't source ~/.bashrc). Fix by "
            "re-registering with an explicit `--env PATH=...` that includes "
            "/usr/local/go/bin and $HOME/go/bin.",
            api=api,
            searched_path=searched,
        )

    module = catalog_module.install_module(api)
    if module is None:
        raise MaterializationFailed(
            f"Catalog entry for '{api}' has no install_module; cannot auto-install. "
            "Either run `go install <module>@latest` manually, or update the catalog.",
            api=api,
        )

    if on_progress:
        on_progress(f"installing {_binary_name(api)} via go install (first call, can take ~20s)")

    result = subprocess.run(
        [go, "install", f"{module}@latest"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise MaterializationFailed(
            f"go install failed for '{api}' ({module})",
            api=api,
            module=module,
            stderr=_tail(result.stderr, 2000),
        )

    found = cached_binary(api)
    if found is None:
        raise MaterializationFailed(
            f"go install succeeded but {_binary_name(api)} not found on PATH. "
            "Verify $GOPATH/bin is on $PATH.",
            api=api,
            search_path=path_with_gobin(),
        )

    _ensure_agent_context(found, api)
    return found


def _ensure_agent_context(binary: Path, api: str) -> None:
    """Capture <binary> agent-context for catalog enrichment if not already
    cached. Best-effort: failures here don't break the call path, they just
    leave discovery API-level for this CLI."""
    from . import agent_context
    if agent_context.cached(api) is None:
        agent_context.capture(binary, api)


def _tail(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else "…" + text[-limit:]
