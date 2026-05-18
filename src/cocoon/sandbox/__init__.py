"""Per-call sandbox execution. Dispatches to the OS-native backend.

Linux uses bubblewrap with namespace isolation; macOS uses Seatbelt
(`sandbox-exec`) with a dynamically generated SBPL profile. Windows is
not supported in v1.
"""

import subprocess
import sys

from ..errors import SandboxUnavailable
from .policy import SandboxPolicy


def execute(policy: SandboxPolicy) -> subprocess.CompletedProcess:
    if sys.platform == "linux":
        from . import linux
        return linux.execute(policy)
    if sys.platform == "darwin":
        from . import macos
        return macos.execute(policy)
    raise SandboxUnavailable(
        f"sandbox not supported on platform '{sys.platform}'",
        platform=sys.platform,
    )


def probe() -> dict[str, object]:
    """Diagnostic snapshot for `cocoon doctor`. Doesn't raise."""
    import shutil
    if sys.platform == "linux":
        path = shutil.which("bwrap")
        return {"backend": "bwrap", "available": path is not None, "path": path or ""}
    if sys.platform == "darwin":
        path = shutil.which("sandbox-exec")
        return {"backend": "sandbox-exec", "available": path is not None, "path": path or ""}
    return {"backend": "none", "available": False, "path": ""}


__all__ = ["SandboxPolicy", "execute", "probe"]
