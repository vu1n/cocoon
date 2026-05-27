"""macOS backend: Seatbelt via sandbox-exec with a dynamic SBPL profile.

Note: sandbox-exec is officially deprecated by Apple (has been for years)
but remains functional and is what Codex and Claude Code both use. The
profile allows file reads broadly — `(allow file-read*)` — because Go
binaries on macOS need wide file-read* + mach* access for the runtime;
a scoped read allow-list (system dirs + binary only) reliably SIGABRTs
generated Go CLIs, so it's not a viable containment lever here.

Containment is therefore expressed as the inverse: env-scrubbing isolates
tokens passed via the environment, and `policy.deny_read_paths` emits
`(deny file-read* (subpath ...))` AFTER the blanket allow to carve out the
cross-API credential stores (~/.cache/cocoon/auth, ~/.press-auth). Seatbelt
honors the later deny over the earlier allow, so a sandboxed CLI can read
the system + its own files but not another API's token off disk — the one
leak env-scrubbing alone can't prevent. Whether a CLI tolerates running
without its real $HOME is what the conformance probe measures.
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
    # Carve credential stores back out, emitted after the writable allows so
    # Seatbelt's later-match-wins resolution blocks them even if a writable
    # path (whose file* implies file-read*) overlaps a denied subpath.
    for path in policy.deny_read_paths:
        lines.append(f'(deny file-read* (subpath "{path}"))')
    # Re-expose explicitly-projected read paths LAST, so a precise credential
    # file stays readable even though its parent store is denied above
    # (e.g. one delegated API's ~/.press-auth/<domain>.json while the rest of
    # the store is denied). readable_paths = "expose exactly this";
    # deny_read_paths = "hide the rest".
    for path in policy.readable_paths:
        lines.append(f'(allow file-read* (subpath "{path}"))')
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
