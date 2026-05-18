"""Lazy CLI materialization via printing-press.

On first call to any API, shells out to `printing-press <api>` to generate
and build the Go CLI, then caches the binary under ~/.cache/cocoon/binaries/<api>/.
Subsequent calls hit the cache directly.

The printing-press codegen itself is invoked unsandboxed in v0: it needs
network access (to fetch the OpenAPI spec) and write access to the cache
directory. v1.1 will sandbox the codegen step too once the bwrap profile
for Go module fetch + compile is worked out.

Optional `on_progress` callback lets the MCP server emit progress
notifications while the build runs (the SKILL promises this so hosts
can show a spinner during the tens-of-seconds first call).
"""

import shutil
import subprocess
from pathlib import Path
from typing import Callable

from .errors import MaterializationFailed
from .paths import binaries_dir

ProgressCallback = Callable[[str], None]


def _binary_path(api: str) -> Path:
    return binaries_dir() / api / f"{api}-pp-cli"


def cached_binary(api: str) -> Path | None:
    path = _binary_path(api)
    return path if path.exists() else None


def materialize(api: str, *, on_progress: ProgressCallback | None = None) -> Path:
    binary = _binary_path(api)
    if binary.exists():
        return binary

    pp = shutil.which("printing-press")
    if pp is None:
        raise MaterializationFailed(
            f"printing-press not installed; cannot materialize CLI for '{api}'. "
            "See https://github.com/mvanhorn/cli-printing-press for install instructions.",
            api=api,
        )

    binary.parent.mkdir(parents=True, exist_ok=True)

    if on_progress:
        on_progress(f"materializing {api} CLI (first call, ~30s)")

    result = subprocess.run(
        [pp, api, "--output", str(binary.parent)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise MaterializationFailed(
            f"printing-press codegen failed for '{api}'",
            api=api,
            stderr=_tail(result.stderr, 2000),
        )
    if not binary.exists():
        raise MaterializationFailed(
            f"printing-press completed but expected binary not found at {binary}",
            api=api,
            expected_path=str(binary),
        )
    binary.chmod(0o755)
    return binary


def _tail(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else "…" + text[-limit:]
