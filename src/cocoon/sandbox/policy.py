"""SandboxPolicy: one declarative shape, two OS backends.

Modeled on Codex's SandboxPolicy abstraction — a single dataclass that each
OS-native backend translates into bwrap flags (Linux) or an SBPL profile
(macOS). The policy carries only what the backend needs; transport concerns
(timeouts, capture-or-stream) are passed alongside as keyword args.
"""

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class SandboxPolicy:
    binary: Path
    argv: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    writable_paths: tuple[Path, ...] = ()
    network: bool = False
    timeout: float = 60.0
