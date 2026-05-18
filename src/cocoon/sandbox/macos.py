"""macOS backend: Seatbelt via sandbox-exec with a dynamic SBPL profile.

Note: sandbox-exec is officially deprecated by Apple (has been for years)
but remains functional and is what Codex and Claude Code both use. The
profile here is intentionally loose because Go binaries on macOS need
broad file-read* + mach* access for the runtime; tighter policies reliably
break generated CLIs. We rely on the env-scrubbing (controlled subprocess
env) for token isolation, not on Seatbelt for file containment.
"""

import os
import shutil
import subprocess
import tempfile

from ..errors import SandboxUnavailable
from .policy import SandboxPolicy


def _resolve_sandbox_exec() -> str:
    found = shutil.which("sandbox-exec")
    if found is None:
        raise SandboxUnavailable("sandbox-exec not available (macOS only)")
    return found


def build_sbpl(policy: SandboxPolicy) -> str:
    lines = [
        "(version 1)",
        "(deny default)",
        "(allow process-fork)",
        f'(allow process-exec (literal "{policy.binary}"))',
        "(allow file-read*)",
        "(allow mach-lookup)",
        "(allow ipc-posix-shm)",
        "(allow sysctl-read)",
        "(allow signal (target self))",
    ]
    for rw in policy.writable_paths:
        lines.append(f'(allow file* (subpath "{rw}"))')
    if policy.network:
        lines.append("(allow network*)")
    return "\n".join(lines) + "\n"


def execute(policy: SandboxPolicy) -> subprocess.CompletedProcess:
    sbpl = build_sbpl(policy)
    with tempfile.NamedTemporaryFile("w", suffix=".sb", delete=False, encoding="utf-8") as f:
        f.write(sbpl)
        profile_path = f.name
    try:
        # Seatbelt has no per-process env scoping like bwrap's --setenv,
        # so we control the subprocess env directly: minimal PATH + policy env.
        env = {"PATH": "/usr/bin:/bin", **policy.env}
        return subprocess.run(
            [_resolve_sandbox_exec(), "-f", profile_path, str(policy.binary), *policy.argv],
            capture_output=True,
            text=True,
            timeout=policy.timeout,
            env=env,
        )
    finally:
        os.unlink(profile_path)
