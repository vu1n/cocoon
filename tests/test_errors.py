import pytest

from cocoon.errors import AuthMissing, CocoonError, MaterializationFailed


def test_code_is_class_level_and_overridden() -> None:
    assert CocoonError.code == "cocoon_error"
    assert MaterializationFailed.code == "materialization_failed"
    assert AuthMissing.code == "auth_missing"


def test_detail_dict_carries_kwargs() -> None:
    err = AuthMissing("nope", api="linear", path="/x", setup_hint="cocoon auth ...")
    assert err.detail == {"api": "linear", "path": "/x", "setup_hint": "cocoon auth ..."}
    assert err.message == "nope"


def test_to_dict_shape_is_stable() -> None:
    err = MaterializationFailed("boom", api="slack", stderr="oops")
    assert err.to_dict() == {
        "error": "materialization_failed",
        "message": "boom",
        "detail": {"api": "slack", "stderr": "oops"},
    }


def test_can_be_raised_and_caught_as_exception() -> None:
    with pytest.raises(CocoonError) as info:
        raise AuthMissing("x", api="a")
    assert info.value.code == "auth_missing"


def test_str_returns_message() -> None:
    assert str(MaterializationFailed("kaboom")) == "kaboom"
