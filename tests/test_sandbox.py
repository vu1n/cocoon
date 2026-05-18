from pathlib import Path

from cocoon.sandbox import linux, macos
from cocoon.sandbox.policy import SandboxPolicy


def _policy(**overrides) -> SandboxPolicy:
    defaults: dict = {
        "binary": Path("/tmp/fake-cli"),
        "argv": ("issues", "list"),
        "env": {"TOKEN": "secret"},
    }
    return SandboxPolicy(**(defaults | overrides))


class TestLinuxArgv:
    def test_default_isolates_network(self) -> None:
        argv = linux.build_argv(_policy())
        assert "--unshare-net" in argv
        assert "--unshare-user" in argv
        assert "--unshare-pid" in argv

    def test_network_true_omits_unshare_net(self) -> None:
        argv = linux.build_argv(_policy(network=True))
        assert "--unshare-net" not in argv

    def test_env_is_cleared_then_set(self) -> None:
        argv = linux.build_argv(_policy())
        assert "--clearenv" in argv
        token_idx = argv.index("TOKEN")
        assert token_idx > argv.index("--clearenv")

    def test_binary_appears_after_separator(self) -> None:
        argv = linux.build_argv(_policy())
        sep = argv.index("--")
        assert argv[sep + 1] == "/tmp/fake-cli"
        assert argv[sep + 2 :] == ["issues", "list"]

    def test_writable_paths_bound_rw(self) -> None:
        argv = linux.build_argv(_policy(writable_paths=(Path("/tmp/work"),)))
        bind_idx = argv.index("--bind")
        assert argv[bind_idx + 1] == "/tmp/work"
        assert argv[bind_idx + 2] == "/tmp/work"


class TestMacosSBPL:
    def test_includes_binary_literal(self) -> None:
        sbpl = macos.build_sbpl(_policy())
        assert '(allow process-exec (literal "/tmp/fake-cli"))' in sbpl

    def test_deny_default(self) -> None:
        assert "(deny default)" in macos.build_sbpl(_policy())

    def test_network_off_by_default(self) -> None:
        assert "(allow network*)" not in macos.build_sbpl(_policy())

    def test_network_true_grants_network(self) -> None:
        assert "(allow network*)" in macos.build_sbpl(_policy(network=True))

    def test_writable_path_gets_subpath_allow(self) -> None:
        sbpl = macos.build_sbpl(_policy(writable_paths=(Path("/tmp/work"),)))
        assert '(allow file* (subpath "/tmp/work"))' in sbpl
