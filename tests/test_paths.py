from pathlib import Path

from cocoon import paths


def test_cache_root_respects_env_override(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("COCOON_CACHE_DIR", str(tmp_path / "custom"))
    assert paths.cache_root() == tmp_path / "custom"


def test_getters_are_pure(tmp_path: Path, monkeypatch) -> None:
    """No side effects: getters compute paths but don't create directories."""
    monkeypatch.setenv("COCOON_CACHE_DIR", str(tmp_path / "fresh"))
    paths.auth_dir()
    paths.catalog_dir()
    paths.agent_context_dir()
    paths.binaries_dir()
    assert not (tmp_path / "fresh").exists()


def test_ensure_dirs_creates_all(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("COCOON_CACHE_DIR", str(tmp_path))
    paths.ensure_dirs()
    assert paths.auth_dir().is_dir()
    assert paths.catalog_dir().is_dir()
    assert paths.agent_context_dir().is_dir()
    assert paths.binaries_dir().is_dir()
