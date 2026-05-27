"""Conformance-probe ladder logic, tested with injected fakes (no network,
no real binaries, no real sandbox)."""

import subprocess
from pathlib import Path

from cocoon import conformance
from cocoon.errors import MaterializationFailed, SandboxUnavailable
from cocoon.sandbox import SandboxPolicy


def _proc(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["x"], returncode=returncode, stdout=stdout, stderr=stderr)


def _tier_label(policy: SandboxPolicy) -> str:
    """Recover which tier built this policy — mirrors conformance.TIERS so
    the fake runner can decide pass/fail per tier. The credential denial is
    constant across tiers, so the discriminators are HOME + network."""
    if policy.network:
        return "network"
    # real-home tiers point HOME at the real home; synthetic at the ephemeral
    # dir under the cocoon cache (prefix "probe-home-").
    if "probe-home-" not in policy.env["HOME"]:
        return "real_home"
    return "synthetic_home"


class _Runner:
    """Fake sandbox runner. Succeeds for tiers whose label is in `passes`;
    records every tier it was asked to run so tests can assert early-stop."""

    def __init__(self, passes: set[str], *, raise_on: dict | None = None):
        self.passes = passes
        self.raise_on = raise_on or {}
        self.calls: list[str] = []

    def __call__(self, policy: SandboxPolicy) -> subprocess.CompletedProcess:
        label = _tier_label(policy)
        self.calls.append(label)
        if label in self.raise_on:
            raise self.raise_on[label]
        if label in self.passes:
            return _proc(0, stdout='{"commands": []}')
        return _proc(1, stderr=f"boom in {label}")


def _materializer(_api: str) -> Path:
    return Path("/Users/me/.cache/cocoon/bin/x/x-pp-cli")


def _probe(api="x", *, runner, materializer=_materializer, home_root=None):
    return conformance.probe(
        api, materializer=materializer, runner=runner, home_root=home_root
    )


class TestLadder:
    def test_tightest_pass_stops_immediately(self, tmp_path: Path) -> None:
        runner = _Runner(passes={"synthetic_home"})
        out = _probe(runner=runner, home_root=tmp_path)
        assert out.status == conformance.STATUS_READY
        assert out.tier == "synthetic_home"
        assert runner.calls == ["synthetic_home"]  # didn't bother loosening

    def test_falls_through_to_real_home(self, tmp_path: Path) -> None:
        runner = _Runner(passes={"real_home", "network"})
        out = _probe(runner=runner, home_root=tmp_path)
        assert out.status == conformance.STATUS_READY
        assert out.tier == "real_home"
        assert "$HOME" in out.finding
        assert runner.calls == ["synthetic_home", "real_home"]

    def test_network_only_finding_names_network(self, tmp_path: Path) -> None:
        out = _probe(runner=_Runner(passes={"network"}), home_root=tmp_path)
        assert out.tier == "network"
        assert "network" in out.finding

    def test_all_tiers_fail(self, tmp_path: Path) -> None:
        runner = _Runner(passes=set())
        out = _probe(runner=runner, home_root=tmp_path)
        assert out.status == conformance.STATUS_FAILED
        assert out.tier is None
        assert out.returncode == 1
        assert "boom in network" in out.stderr_excerpt  # last tier's stderr
        assert runner.calls == ["synthetic_home", "real_home", "network"]

    def test_timeout_continues_ladder(self, tmp_path: Path) -> None:
        runner = _Runner(
            passes={"real_home"},
            raise_on={"synthetic_home": subprocess.TimeoutExpired(cmd="x", timeout=30)},
        )
        out = _probe(runner=runner, home_root=tmp_path)
        assert out.status == conformance.STATUS_READY
        assert out.tier == "real_home"

    def test_empty_stdout_is_not_a_pass(self, tmp_path: Path) -> None:
        # exit 0 but nothing emitted shouldn't count as conformant.
        class Empty(_Runner):
            def __call__(self, policy):
                self.calls.append(_tier_label(policy))
                return _proc(0, stdout="   ")

        out = _probe(runner=Empty(passes=set()), home_root=tmp_path)
        assert out.status == conformance.STATUS_FAILED


class TestAgentContextFallback:
    """CLIs lacking agent-context (docs/sniff-driven) must not be misread as
    sandbox failures — the probe falls back to --help and flags the gap."""

    class _NoAgentCtx:
        def __init__(self):
            self.calls: list[tuple] = []

        def __call__(self, policy):
            self.calls.append((_tier_label(policy), policy.argv))
            if policy.argv == conformance.PROBE_ARGV:
                return _proc(1, stderr='Error: unknown command "agent-context"')
            return _proc(0, stdout="Usage: x [command]")

    def test_falls_back_to_help_and_flags_gap(self, tmp_path: Path) -> None:
        runner = self._NoAgentCtx()
        out = conformance.probe("x", materializer=_materializer, runner=runner,
                                home_root=tmp_path)
        assert out.status == conformance.STATUS_READY
        assert out.tier == "synthetic_home"
        assert out.probe_verb == "--help"
        assert "agent-context" in out.finding  # surfaces the enrichment gap
        # first tier tried agent-context, then retried the same tier with --help
        assert runner.calls[0] == ("synthetic_home", conformance.PROBE_ARGV)
        assert runner.calls[1] == ("synthetic_home", conformance.FALLBACK_ARGV)

    def test_real_failure_does_not_trigger_fallback(self, tmp_path: Path) -> None:
        # A non-"unknown command" failure is a genuine sandbox failure; the
        # probe must not paper over it with a --help retry. _Runner fails with
        # "boom in <tier>" (no "unknown command"), so each tier runs exactly
        # once — 3 tiers, 3 calls, no fallback doubling.
        runner = _Runner(passes=set())
        out = _probe(runner=runner, home_root=tmp_path)
        assert out.status == conformance.STATUS_FAILED
        assert len(runner.calls) == len(conformance.TIERS)


class TestNonSandboxOutcomes:
    def test_materialize_failure_is_unavailable(self, tmp_path: Path) -> None:
        def boom(_api):
            raise MaterializationFailed("no asset", api="x")

        out = _probe(runner=_Runner(passes={"synthetic_home"}), materializer=boom,
                     home_root=tmp_path)
        assert out.status == conformance.STATUS_UNAVAILABLE
        assert out.tier is None

    def test_sandbox_unavailable_is_skipped(self, tmp_path: Path) -> None:
        runner = _Runner(passes=set(),
                         raise_on={"synthetic_home": SandboxUnavailable("no bwrap")})
        out = _probe(runner=runner, home_root=tmp_path)
        assert out.status == conformance.STATUS_SKIPPED


class TestPolicyForTier:
    DENY = (Path("/Users/me/.cache/cocoon/auth"), Path("/Users/me/.press-auth"))

    def _build(self, tier_name: str) -> SandboxPolicy:
        tier = next(t for t in conformance.TIERS if t.name == tier_name)
        return conformance._policy_for_tier(
            Path("/bin/x"), tier,
            synthetic_home=Path("/synth"), real_home=Path("/Users/me"),
            deny_read_paths=self.DENY,
        )

    def test_credential_dirs_denied_in_every_tier(self) -> None:
        # The isolation invariant: no tier ever exposes the credential stores.
        for tier in conformance.TIERS:
            assert self._build(tier.name).deny_read_paths == self.DENY

    def test_synthetic_home_tier(self) -> None:
        p = self._build("synthetic_home")
        assert p.network is False
        assert p.env["HOME"] == "/synth"
        assert p.writable_paths == (Path("/synth"),)
        assert p.argv == conformance.PROBE_ARGV

    def test_real_home_tier_points_home_at_real(self) -> None:
        p = self._build("real_home")
        assert p.env["HOME"] == "/Users/me"
        # synthetic dir stays writable so config-writing CLIs don't fail
        assert p.writable_paths == (Path("/synth"),)
        assert p.network is False

    def test_network_tier(self) -> None:
        assert self._build("network").network is True


def test_outcome_to_dict_roundtrips() -> None:
    out = conformance.ProbeOutcome(
        api="linear", status=conformance.STATUS_READY, tier="synthetic_home",
        finding="ok", returncode=0, stderr_excerpt="", duration_s=1.2345,
    )
    d = out.to_dict()
    assert d["api"] == "linear"
    assert d["tier"] == "synthetic_home"
    assert d["duration_s"] == 1.234  # rounded to 3dp
    assert out.ok is True
