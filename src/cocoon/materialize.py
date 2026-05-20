"""Seamless install + lookup of printing-press CLIs via prebuilt binaries.

The agent never has to take a separate install step. When `call_capability`
runs against an API whose binary isn't cached, cocoon downloads the
per-platform prebuilt binary from printing-press-library's GitHub release
(tag `<api>-current`) and caches it under `~/.cache/cocoon/bin/<api>/`.
The cached binary is invoked through the per-call sandbox as before.

Why prebuilt over `go install`:
- No Go toolchain dependency on the host (was the v0.3 setup).
- No host-PATH-inheritance gotcha for MCP subprocesses (the bite the
  hermes postmortem's RC1 documented).
- ~5-10x faster cold start (download ~12MB instead of fetch source +
  compile).

The download itself runs unsandboxed (it needs network + write to the
cache dir, and the curated catalog is the trust boundary). v1.1 may
add sha256 verification once upstream publishes a checksums manifest
(goreleaser is configured for it, just not currently uploaded).
"""

import os
import platform
import sys
from pathlib import Path
from typing import Callable

import httpx

from . import catalog as catalog_module
from .errors import MaterializationFailed
from .paths import binaries_dir

ProgressCallback = Callable[[str], None]

RELEASE_BASE = "https://github.com/mvanhorn/printing-press-library/releases/download"


def _binary_name(api: str) -> str:
    return f"{api}-pp-cli"


def _binary_path(api: str) -> Path:
    return binaries_dir() / api / _binary_name(api)


def cached_binary(api: str) -> Path | None:
    """Returns the cocoon-managed cache path for an installed binary, or
    None if absent. Unlike v0.3, this does NOT consult `$GOPATH/bin` —
    cocoon owns the cache exclusively, dodging the host-PATH gotcha that
    bit `go install`-era materialize."""
    path = _binary_path(api)
    return path if path.exists() else None


def materialize(api: str, *, on_progress: ProgressCallback | None = None) -> Path:
    existing = cached_binary(api)
    if existing:
        # Users who first-installed before cocoon, or whose cache survived
        # a cocoon upgrade, would otherwise never get the agent-context
        # dump for catalog enrichment. Ensure it's captured on every
        # materialize, gated by a cheap exists-check inside.
        _ensure_agent_context(existing, api)
        return existing

    # The agent passes the api key (e.g. "hackernews"). printing-press
    # uses the same string as the registry name and the release-asset
    # prefix, so api == name == download URL segment. If the catalog
    # doesn't have this api at all, we can't safely guess a URL.
    if catalog_module.install_module(api) is None:
        raise MaterializationFailed(
            f"Catalog has no entry for api '{api}'; nothing to download.",
            api=api,
        )
    name = api

    try:
        os_seg, arch_seg = _platform_segments()
    except _UnsupportedPlatform as exc:
        raise MaterializationFailed(
            f"Cannot materialize on this platform: {exc}. "
            "printing-press-library publishes binaries for "
            "linux/darwin/windows × amd64/arm64.",
            api=api,
            platform=sys.platform,
            arch=platform.machine(),
        ) from exc

    asset = f"{name}-pp-cli-{os_seg}-{arch_seg}"
    if os_seg == "windows":
        asset += ".exe"
    url = f"{RELEASE_BASE}/{name}-current/{asset}"

    if on_progress:
        on_progress(f"downloading {asset} (first call, ~2-3s)")

    target = _binary_path(api)
    target.parent.mkdir(parents=True, exist_ok=True)
    _download(url, target, api=api)
    target.chmod(0o755)

    _ensure_agent_context(target, api)
    return target


def _download(url: str, target: Path, *, api: str) -> None:
    """Stream a release asset to disk. Atomic-ish: write to .partial then
    rename, so a half-downloaded binary never gets exec'd if the call is
    interrupted."""
    partial = target.parent / (target.name + ".partial")
    try:
        with httpx.stream("GET", url, follow_redirects=True, timeout=60.0) as response:
            if response.status_code == 404:
                raise MaterializationFailed(
                    f"No release asset at {url} (404). The '{api}-current' tag "
                    "may not exist yet, or the asset name doesn't match the "
                    "platform/arch combination this host needs.",
                    api=api,
                    url=url,
                )
            response.raise_for_status()
            with open(partial, "wb") as f:
                for chunk in response.iter_bytes(chunk_size=64 * 1024):
                    f.write(chunk)
    except httpx.HTTPError as exc:
        partial.unlink(missing_ok=True)
        raise MaterializationFailed(
            f"Download failed for '{api}': {exc}",
            api=api,
            url=url,
        ) from exc
    os.replace(partial, target)


def _ensure_agent_context(binary: Path, api: str) -> None:
    """Capture <binary> agent-context for catalog enrichment if not already
    cached. Best-effort: failures here don't break the call path, they just
    leave discovery API-level for this CLI."""
    from . import agent_context
    if agent_context.cached(api) is None:
        agent_context.capture(binary, api)


# ---------------------------------------------------------------------------
# Platform detection. Upstream's goreleaser publishes the cross-product of:
#   os:   linux, darwin, windows
#   arch: amd64, arm64
# We map sys.platform and platform.machine() into that vocabulary.
# ---------------------------------------------------------------------------


class _UnsupportedPlatform(Exception):
    pass


def _platform_segments() -> tuple[str, str]:
    os_seg = _os_segment()
    arch_seg = _arch_segment()
    return os_seg, arch_seg


def _os_segment() -> str:
    if sys.platform == "linux":
        return "linux"
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform == "win32":
        return "windows"
    raise _UnsupportedPlatform(f"unknown os '{sys.platform}'")


def _arch_segment() -> str:
    machine = platform.machine().lower()
    # x86_64 / amd64 (Linux/macOS Intel and Windows AMD64)
    if machine in ("x86_64", "amd64"):
        return "amd64"
    # arm64 (macOS Apple Silicon, Windows ARM64) / aarch64 (Linux ARM64)
    if machine in ("arm64", "aarch64"):
        return "arm64"
    raise _UnsupportedPlatform(f"unknown arch '{platform.machine()}'")
