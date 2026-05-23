from cocoon import auth_recipes


def test_recipe_for_known_api_returns_dict() -> None:
    recipe = auth_recipes.recipe_for("airbnb")
    assert recipe is not None
    assert recipe["method"] == "browser_session"
    assert recipe["env_var"] == "AIRBNB_COOKIE"
    assert recipe["login_url"].startswith("https://")


def test_recipe_for_unknown_api_returns_none() -> None:
    """Most APIs don't have a recipe yet — None is the expected
    'no automated guidance' signal."""
    assert auth_recipes.recipe_for("definitely-not-in-recipes") is None


def test_load_recipes_only_returns_recipes_namespace() -> None:
    """The bundled JSON has `_meta` and `recipes` at top level.
    load_recipes() returns only the recipes — meta never leaks into
    the lookup surface."""
    recipes = auth_recipes.load_recipes()
    assert "airbnb" in recipes
    assert "_meta" not in recipes
    assert "_comment" not in recipes
