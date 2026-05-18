from cocoon.argv import args_to_argv, tool_argv


class TestArgsToArgv:
    def test_string_uses_equals_form(self) -> None:
        assert args_to_argv({"channel": "#general"}) == ("--channel=#general",)

    def test_underscore_keys_become_dashes(self) -> None:
        assert args_to_argv({"team_id": "abc"}) == ("--team-id=abc",)

    def test_true_becomes_bare_flag(self) -> None:
        assert args_to_argv({"verbose": True}) == ("--verbose",)

    def test_false_is_dropped(self) -> None:
        assert args_to_argv({"verbose": False}) == ()

    def test_none_is_dropped(self) -> None:
        assert args_to_argv({"opt": None, "req": "x"}) == ("--req=x",)

    def test_nested_objects_json_encoded_compactly(self) -> None:
        assert args_to_argv({"filter": {"state": "open"}}) == ('--filter={"state":"open"}',)

    def test_integers_passed_through(self) -> None:
        assert args_to_argv({"limit": 10}) == ("--limit=10",)


class TestToolArgv:
    def test_dotted_tool_splits_into_subcommands(self) -> None:
        assert tool_argv("issues.create", {"title": "x"}) == ("issues", "create", "--title=x")

    def test_single_segment_tool_is_one_subcommand(self) -> None:
        assert tool_argv("ping", {}) == ("ping",)

    def test_empty_args_dict_omitted(self) -> None:
        assert tool_argv("issues.list", None) == ("issues", "list")

    def test_args_appended_in_insertion_order(self) -> None:
        out = tool_argv("issues.list", {"team_id": "t1", "limit": 5})
        assert out == ("issues", "list", "--team-id=t1", "--limit=5")
