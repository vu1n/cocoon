"""Unit tests for materialize.py — the prebuilt-binary download path.

Platform detection and URL construction are pure functions; we cover those
directly. The actual HTTP download is verified end-to-end by scripts/e2e_smoke.py
(it hits real GitHub Releases), so unit tests only mock httpx to verify error
paths and the cache layout cocoon writes."""

import platform
from pathlib import Path
from unittest.mock import patch

import pytest

from cocoon import materialize


# ---------------------------------------------------------------------------
# Platform → URL segment mapping (pure functions)
# ---------------------------------------------------------------------------


class TestPlatformSegments:
    def test_linux_amd64(self) -> None:
        with patch.object(materialize.sys, "platform", "linux"), \
             patch.object(materialize.platform, "machine", return_value="x86_64"):
            assert materialize._platform_segments() == ("linux", "amd64")

    def test_linux_arm64(self) -> None:
        with patch.object(materialize.sys, "platform", "linux"), \
             patch.object(materialize.platform, "machine", return_value="aarch64"):
            assert materialize._platform_segments() == ("linux", "arm64")

    def test_darwin_apple_silicon(self) -> None:
        with patch.object(materialize.sys, "platform", "darwin"), \
             patch.object(materialize.platform, "machine", return_value="arm64"):
            assert materialize._platform_segments() == ("darwin", "arm64")

    def test_darwin_intel(self) -> None:
        with patch.object(materialize.sys, "platform", "darwin"), \
             patch.object(materialize.platform, "machine", return_value="x86_64"):
            assert materialize._platform_segments() == ("darwin", "amd64")

    def test_windows_amd64(self) -> None:
        with patch.object(materialize.sys, "platform", "win32"), \
             patch.object(materialize.platform, "machine", return_value="AMD64"):
            assert materialize._platform_segments() == ("windows", "amd64")

    def test_windows_arm64(self) -> None:
        with patch.object(materialize.sys, "platform", "win32"), \
             patch.object(materialize.platform, "machine", return_value="ARM64"):
            assert materialize._platform_segments() == ("windows", "arm64")

    def test_unsupported_os_raises(self) -> None:
        with patch.object(materialize.sys, "platform", "freebsd"), \
             pytest.raises(materialize._UnsupportedPlatform):
            materialize._platform_segments()

    def test_unsupported_arch_raises(self) -> None:
        with patch.object(materialize.sys, "platform", "linux"), \
             patch.object(materialize.platform, "machine", return_value="riscv64"), \
             pytest.raises(materialize._UnsupportedPlatform):
            materialize._platform_segments()


# ---------------------------------------------------------------------------
# Cache-path resolution
# ---------------------------------------------------------------------------


def test_binary_path_under_cache_root(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("COCOON_CACHE_DIR", str(tmp_path))
    assert materialize._binary_path("hackernews") == (
        tmp_path / "bin" / "hackernews" / "hackernews-pp-cli"
    )


def test_cached_binary_returns_none_when_missing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("COCOON_CACHE_DIR", str(tmp_path))
    assert materialize.cached_binary("never-downloaded") is None


def test_cached_binary_returns_path_when_present(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("COCOON_CACHE_DIR", str(tmp_path))
    binary = tmp_path / "bin" / "hackernews" / "hackernews-pp-cli"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"\x7fELF...")
    assert materialize.cached_binary("hackernews") == binary


# ---------------------------------------------------------------------------
# Materialize error paths (the happy path is covered by e2e_smoke against
# real GitHub Releases, so unit tests focus on the failure modes that
# shouldn't require network).
# ---------------------------------------------------------------------------


def test_materialize_errors_when_api_not_in_catalog(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("COCOON_CACHE_DIR", str(tmp_path))
    monkeypatch.delenv("COCOON_CATALOG_URL", raising=False)
    from cocoon.errors import MaterializationFailed
    with pytest.raises(MaterializationFailed) as info:
        materialize.materialize("totally-fake-api-not-in-catalog")
    assert "no entry" in info.value.message.lower()


def test_materialize_errors_on_unsupported_platform(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("COCOON_CACHE_DIR", str(tmp_path))
    from cocoon.errors import MaterializationFailed
    with patch.object(materialize.sys, "platform", "freebsd"), \
         pytest.raises(MaterializationFailed) as info:
        materialize.materialize("hackernews")
    assert info.value.detail.get("platform") == "freebsd"
