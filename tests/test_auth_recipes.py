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


def test_load_recipes_strips_comment_keys() -> None:
    """The bundled JSON carries a `_comment` describing the file's
    purpose; that key shouldn't leak into the recipe lookup surface."""
    recipes = auth_recipes.load_recipes()
    assert not any(k.startswith("_") for k in recipes)
    assert "airbnb" in recipes
