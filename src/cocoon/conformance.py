"""Conformance probe: which printing-press CLIs run cleanly in cocoon's
tightened sandbox?

Cocoon's value is sandboxed, scoped-auth execution of CLIs it doesn't own.
Two seams are currently loose — macOS file reads are unrestricted
(`file-read*` allow-all) and the delegated cookie-auth path hands the CLI a
real `$HOME`. Before tightening those (scoped reads + projected credentials),
we need to know which of the ~167 catalogued CLIs actually tolerate the tight
policy and which need a loosened lever (or a shim / upstream fix).

This module answers that per-API by running each CLI's `agent-context`
subcommand — a no-auth, local, read-only operation every printing-press CLI
exposes — under a ladder of progressively looser policies, recording the
TIGHTEST tier under which it succeeds. Every tier denies read of the
cross-API credential stores (deny_read_paths), so any `ready` verdict is a
SECURE config — the tiers differ only in how much else the CLI is given:

    synthetic_home   synthetic ephemeral HOME, no network   (the target)
    real_home        real $HOME exposed, no network
    network          real $HOME, network on

A CLI that passes `synthetic_home` is fully conformant: it runs without ever
seeing the real home, so it can't read the user's other dotfiles either. One
that only passes a looser tier names the exact lever it needs:

    real_home → reads its own config/state from the real $HOME
    network   → reaches the network even to describe itself

(An earlier design scoped reads to an allow-list excluding $HOME; the probe
itself proved that SIGABRTs Go CLIs on macOS, so containment is expressed as
deny-credential-dirs instead — see sandbox/macos.py.)

`agent-context` deliberately exercises startup + filesystem + reads WITHOUT
the auth/network call path, isolating the sandbox-environment question from
the credential question. A synthetic_home-clean CLI here still needs its real
call path probed once credential projection is wired — that's future work;
this probe scopes the sandbox-environment axis only.

Because `materialize` already runs `agent-context` once UNSANDBOXED (for
catalog enrichment), a `failed` result cleanly isolates "the sandbox policy
broke it" from "the binary itself is broken" — the latter would have failed
materialization first.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from . import materialize as materialize_module
from .errors import CocoonError, SandboxUnavailable
from .paths import cache_root, protected_credential_paths
from .sandbox import SandboxPolicy
from .sandbox import execute as sandbox_execute

# Primary probe verb: local, no-auth, no-network, and what cocoon itself runs
# post-install for catalog enrichment — so testing it is representative. Not
# universal, though: docs/sniff-driven CLIs lack it and exit with cobra's
# "unknown command". For those we fall back to `--help` (every cobra CLI has
# it) to still answer the sandbox-tolerance question, and flag the gap.
PROBE_ARGV: tuple[str, ...] = ("agent-context",)
FALLBACK_ARGV: tuple[str, ...] = ("--help",)
_UNKNOWN_COMMAND = "unknown command"

# Per-tier timeout. agent-context is a local dump; anything slower than this
# under the sandbox is itself a signal (likely a network stall on a tier that
# shouldn't need network).
PROBE_TIMEOUT_S: float = 30.0

# Outcome statuses.
STATUS_READY = "ready"              # passed at least one tier
STATUS_FAILED = "failed"            # ran but failed under every tier
STATUS_UNAVAILABLE = "unavailable"  # couldn't materialize (no asset / download error)
STATUS_SKIPPED = "skipped"          # no sandbox backend on this host

_STDERR_EXCERPT_CHARS = 400


@dataclass(frozen=True)
class Tier:
    """One rung of the loosen-until-it-passes ladder. Credential-store denial
    is constant across tiers (enforced in _policy_for_tier), so the levers
    that vary are just: which HOME, and whether network is allowed."""

    name: str
    home: str  # "synthetic" | "real"
    network: bool
    # Human-readable implication when this is the tightest tier a CLI passes.
    finding: str


# Ordered tightest → loosest. The probe stops at the first pass.
TIERS: tuple[Tier, ...] = (
    Tier(
        name="synthetic_home",
        home="synthetic",
        network=False,
        finding="conformant: runs with a synthetic HOME; never sees the real one",
    ),
    Tier(
        name="real_home",
        home="real",
        network=False,
        finding="needs its config/state from the real $HOME (cocoon creds stay blocked)",
    ),
    Tier(
        name="network",
        home="real",
        network=True,
        finding="reaches the network even to emit agent-context",
    ),
)


@dataclass(frozen=True)
class ProbeOutcome:
    api: str
    status: str
    tier: str | None  # tightest passing tier, or None
    finding: str
    returncode: int | None  # exit code of the run that determined status
    stderr_excerpt: str = ""
    duration_s: float = 0.0
    # Which verb actually determined the verdict ("agent-context" or the
    # "--help" fallback). A "--help" verb means the CLI lacks agent-context —
    # a catalog-enrichment gap worth surfacing independent of sandbox fit.
    probe_verb: str = PROBE_ARGV[0]

    @property
    def ok(self) -> bool:
        return self.status == STATUS_READY

    def to_dict(self) -> dict:
        return {
            "api": self.api,
            "status": self.status,
            "tier": self.tier,
            "finding": self.finding,
            "returncode": self.returncode,
            "stderr_excerpt": self.stderr_excerpt,
            "duration_s": round(self.duration_s, 3),
            "probe_verb": self.probe_verb,
        }


Materializer = Callable[[str], Path]
Runner = Callable[[SandboxPolicy], "subprocess.CompletedProcess[str]"]


def _policy_for_tier(
    binary: Path,
    tier: Tier,
    *,
    synthetic_home: Path,
    real_home: Path,
    deny_read_paths: tuple[Path, ...],
) -> SandboxPolicy:
    """Translate a tier into a concrete policy.

    The synthetic-HOME ephemeral dir is always bound writable (CLIs that
    write a config/cache on startup don't fail). The credential stores are
    denied in every tier, so the only thing that loosens as we descend the
    ladder is whether the CLI is handed the real $HOME and the network.
    """
    home = synthetic_home if tier.home == "synthetic" else real_home
    return SandboxPolicy(
        binary=binary,
        argv=PROBE_ARGV,
        env={"HOME": str(home)},
        writable_paths=(synthetic_home,),
        deny_read_paths=deny_read_paths,
        network=tier.network,
        timeout=PROBE_TIMEOUT_S,
    )


def _excerpt(text: str | None) -> str:
    if not text:
        return ""
    text = text.strip()
    return text[-_STDERR_EXCERPT_CHARS:]


def _succeeded(result: "subprocess.CompletedProcess[str]", verb: str) -> bool:
    """A clean run: exit 0. agent-context must also emit a JSON dump (an
    exit-0-but-empty run is anomalous), but the `--help` fallback may print
    usage to stderr, so for it exit 0 alone suffices."""
    if result.returncode != 0:
        return False
    if verb == PROBE_ARGV[0]:
        return bool((result.stdout or "").strip())
    return True


def _run_tier(
    runner: Runner, policy: SandboxPolicy
) -> tuple["subprocess.CompletedProcess[str]", str]:
    """Run one tier's policy, falling back from `agent-context` to `--help`
    when the CLI doesn't have agent-context (cobra "unknown command"). Returns
    the result and the verb that produced it. Sandbox/timeout exceptions
    propagate to the caller's handling."""
    result = runner(policy)
    if result.returncode != 0 and _UNKNOWN_COMMAND in (result.stderr or ""):
        result = runner(replace(policy, argv=FALLBACK_ARGV))
        return result, FALLBACK_ARGV[0]
    return result, PROBE_ARGV[0]


def probe(
    api: str,
    *,
    materializer: Materializer = materialize_module.materialize,
    runner: Runner = sandbox_execute,
    tiers: tuple[Tier, ...] = TIERS,
    home_root: Path | None = None,
) -> ProbeOutcome:
    """Probe one API. Materializes its binary, then runs `agent-context`
    under each tier tightest-first, returning at the first pass.

    Dependencies are injected (materializer, runner) so the ladder logic is
    unit-testable without network or real binaries.
    """
    start = time.monotonic()

    try:
        binary = materializer(api)
    except CocoonError as exc:
        return ProbeOutcome(
            api=api,
            status=STATUS_UNAVAILABLE,
            tier=None,
            finding=f"could not materialize: {exc.message}",
            returncode=None,
            duration_s=time.monotonic() - start,
        )

    real_home = Path(os.environ.get("HOME") or str(Path.home()))
    deny_read_paths = protected_credential_paths()
    base = Path(home_root) if home_root else cache_root()
    base.mkdir(parents=True, exist_ok=True)
    synthetic_home = Path(tempfile.mkdtemp(prefix="probe-home-", dir=base))

    last_rc: int | None = None
    last_err = ""
    last_verb = PROBE_ARGV[0]
    try:
        for tier in tiers:
            policy = _policy_for_tier(
                binary,
                tier,
                synthetic_home=synthetic_home,
                real_home=real_home,
                deny_read_paths=deny_read_paths,
            )
            try:
                result, verb = _run_tier(runner, policy)
            except SandboxUnavailable as exc:
                # No backend on this host — true for every API, so report it
                # plainly rather than as a per-CLI failure.
                return ProbeOutcome(
                    api=api,
                    status=STATUS_SKIPPED,
                    tier=None,
                    finding=exc.message,
                    returncode=None,
                    duration_s=time.monotonic() - start,
                )
            except subprocess.TimeoutExpired:
                last_rc, last_err = None, f"timed out after {policy.timeout}s"
                continue

            last_verb = verb
            if _succeeded(result, verb):
                finding = tier.finding
                if verb == FALLBACK_ARGV[0]:
                    finding += " — but lacks agent-context (probed via --help)"
                return ProbeOutcome(
                    api=api,
                    status=STATUS_READY,
                    tier=tier.name,
                    finding=finding,
                    returncode=result.returncode,
                    stderr_excerpt=_excerpt(result.stderr),
                    duration_s=time.monotonic() - start,
                    probe_verb=verb,
                )
            last_rc, last_err = result.returncode, _excerpt(result.stderr)

        return ProbeOutcome(
            api=api,
            status=STATUS_FAILED,
            tier=None,
            finding="failed under every tier (incl. real $HOME + network)",
            returncode=last_rc,
            stderr_excerpt=last_err,
            duration_s=time.monotonic() - start,
            probe_verb=last_verb,
        )
    finally:
        shutil.rmtree(synthetic_home, ignore_errors=True)
