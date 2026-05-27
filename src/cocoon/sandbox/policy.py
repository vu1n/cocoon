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
    # Read-only paths the CLI may need (e.g. a projected credential file).
    # Linux ro-binds them; macOS already allows them via the blanket read.
    # Distinct from writable_paths so a credential can be exposed read-only.
    readable_paths: tuple[Path, ...] = ()
    network: bool = False
    timeout: float = 60.0
    # Paths the sandboxed CLI must NOT be able to read — used to block the
    # cross-API credential stores (~/.cache/cocoon/auth, ~/.press-auth) so a
    # compromised CLI can't read other APIs' tokens off disk. On macOS this
    # emits `(deny file-read* (subpath ...))` after the blanket allow (a
    # scoped allow-list instead reliably SIGABRTs Go CLIs — see macos.py).
    # On Linux it's informational: reads are bind-scoped, so anything not
    # bound (incl. these) is already invisible. Empty by default, keeping the
    # production call path's behavior unchanged.
    deny_read_paths: tuple[Path, ...] = ()
