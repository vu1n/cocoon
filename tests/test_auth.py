import json
import os
import stat
from pathlib import Path

import pytest

from cocoon import auth
from cocoon.errors import AuthMissing


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("COCOON_CACHE_DIR", str(tmp_path))


def test_round_trip(tmp_path: Path) -> None:
    path = auth.write_token_env("linear", {"LINEAR_TOKEN": "lin_abc"})
    assert Path(path).exists()
    assert auth.load_token_env("linear") == {"LINEAR_TOKEN": "lin_abc"}


def test_written_file_is_user_only_readable(tmp_path: Path) -> None:
    path = Path(auth.write_token_env("slack", {"SLACK_TOKEN": "xoxb-…"}))
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600


def test_missing_raises_with_setup_hint() -> None:
    with pytest.raises(AuthMissing) as info:
        auth.load_token_env("github")
    assert info.value.code == "auth_missing"
    assert info.value.detail["api"] == "github"
    assert "cocoon auth github" in info.value.detail["setup_hint"]


def test_wrong_shape_raises(tmp_path: Path) -> None:
    auth_path = tmp_path / "auth"
    auth_path.mkdir()
    (auth_path / "stripe.json").write_text(json.dumps({"env": "not a dict"}))
    with pytest.raises(AuthMissing):
        auth.load_token_env("stripe")


def test_values_coerced_to_strings(tmp_path: Path) -> None:
    auth_path = tmp_path / "auth"
    auth_path.mkdir()
    (auth_path / "x.json").write_text(json.dumps({"env": {"PORT": 8080}}))
    env = auth.load_token_env("x")
    assert env == {"PORT": "8080"}
