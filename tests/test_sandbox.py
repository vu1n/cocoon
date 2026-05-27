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

    def test_readable_paths_bound_ro(self) -> None:
        argv = linux.build_argv(_policy(readable_paths=(Path("/home/u/cred"),)))
        # the credential gets a --ro-bind-try, not a writable --bind
        joined = list(zip(argv, argv[1:], argv[2:]))
        assert ("--ro-bind-try", "/home/u/cred", "/home/u/cred") in joined

    def test_deny_read_paths_is_noop_on_linux(self) -> None:
        # bwrap reads are bind-scoped regardless; nothing not bound is visible,
        # so deny_read_paths needs no flag and the argv is unchanged.
        with_deny = linux.build_argv(_policy(deny_read_paths=(Path("/home/u/.press-auth"),)))
        assert with_deny == linux.build_argv(_policy())


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

    def test_default_keeps_blanket_read_and_no_deny(self) -> None:
        # Production call path is unchanged: blanket read, no carve-outs.
        sbpl = macos.build_sbpl(_policy())
        assert "(allow file-read*)" in sbpl
        assert "(deny file-read*" not in sbpl


class TestMacosDenyReadPaths:
    """deny_read_paths is how macOS closes the cross-API token leak: blanket
    read stays (Go CLIs need it) but the credential stores are carved out."""

    def _sbpl(self, **overrides):
        auth = Path("/Users/me/.cache/cocoon/auth")
        press = Path("/Users/me/.press-auth")
        return macos.build_sbpl(_policy(deny_read_paths=(auth, press), **overrides))

    def test_keeps_blanket_allow(self) -> None:
        # The allow must remain — scoping reads instead SIGABRTs Go CLIs.
        assert "(allow file-read*)" in self._sbpl()

    def test_emits_deny_for_each_credential_store(self) -> None:
        sbpl = self._sbpl()
        assert '(deny file-read* (subpath "/Users/me/.cache/cocoon/auth"))' in sbpl
        assert '(deny file-read* (subpath "/Users/me/.press-auth"))' in sbpl

    def test_deny_comes_after_blanket_allow_and_writable_allows(self) -> None:
        # Seatbelt is last-match-wins: the deny must come after BOTH the
        # blanket read allow AND any writable `(allow file* ...)` (whose file*
        # implies read), or an overlapping writable path could re-open a
        # denied credential dir.
        sbpl = self._sbpl(writable_paths=(Path("/Users/me/.cache/cocoon/call-home-x"),))
        deny_idx = sbpl.index("(deny file-read*")
        assert sbpl.index("(allow file-read*)") < deny_idx
        assert sbpl.index("(allow file* (subpath") < deny_idx
