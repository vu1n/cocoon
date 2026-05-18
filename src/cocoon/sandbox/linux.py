"""Linux backend: bubblewrap (bwrap) with namespace isolation.

Each call runs with `--unshare-user --unshare-pid`, plus `--unshare-net`
unless the policy explicitly opts in to network. Environment is wiped via
`--clearenv` and only the policy's env vars (plus a minimal PATH) are
restored, so a compromised CLI cannot read other APIs' tokens out of the
process environment.
"""

import shutil
import subprocess

from ..errors import SandboxUnavailable
from .policy import SandboxPolicy


def _resolve_bwrap() -> str:
    found = shutil.which("bwrap")
    if found is None:
        raise SandboxUnavailable(
            "bwrap not installed; install bubblewrap "
            "(`apt install bubblewrap`, `dnf install bubblewrap`, etc.)"
        )
    return found


def build_argv(policy: SandboxPolicy, bwrap_path: str = "bwrap") -> list[str]:
    argv: list[str] = [
        bwrap_path,
        "--die-with-parent",
        "--unshare-user",
        "--unshare-pid",
    ]
    if not policy.network:
        argv += ["--unshare-net"]

    argv += ["--clearenv", "--setenv", "PATH", "/usr/bin:/bin"]
    for key, value in policy.env.items():
        argv += ["--setenv", key, value]

    # Minimal rootfs the generated Go binary needs.
    for path in ("/usr", "/bin"):
        argv += ["--ro-bind", path, path]
    argv += ["--ro-bind-try", "/lib", "/lib"]
    argv += ["--ro-bind-try", "/lib64", "/lib64"]
    argv += ["--ro-bind-try", "/etc/ssl/certs", "/etc/ssl/certs"]
    argv += ["--ro-bind-try", "/etc/resolv.conf", "/etc/resolv.conf"]

    argv += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]

    for rw in policy.writable_paths:
        argv += ["--bind", str(rw), str(rw)]

    argv += ["--ro-bind", str(policy.binary), str(policy.binary)]
    argv += ["--chdir", "/tmp", "--", str(policy.binary), *policy.argv]
    return argv


def execute(policy: SandboxPolicy) -> subprocess.CompletedProcess:
    return subprocess.run(
        build_argv(policy, _resolve_bwrap()),
        capture_output=True,
        text=True,
        timeout=policy.timeout,
    )
