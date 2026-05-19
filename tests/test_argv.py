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


class TestPositionals:
    def test_positional_emitted_before_flags(self) -> None:
        out = tool_argv("items.get", {"itemId": "48182281", "agent": True},
                        positionals=("itemId",))
        assert out == ("items", "get", "48182281", "--agent")

    def test_positional_key_not_re_emitted_as_flag(self) -> None:
        out = tool_argv("items.get", {"itemId": "x"}, positionals=("itemId",))
        assert out == ("items", "get", "x")
        assert "--item-id=x" not in out

    def test_positional_order_follows_declaration(self) -> None:
        out = tool_argv("save.profile", {"value": "42", "name": "default"},
                        positionals=("name", "value"))
        assert out == ("save", "profile", "default", "42")

    def test_positional_missing_from_args_is_skipped(self) -> None:
        out = tool_argv("items.get", {"other": "x"}, positionals=("itemId",))
        assert out == ("items", "get", "--other=x")

    def test_positional_none_value_is_dropped(self) -> None:
        out = tool_argv("items.get", {"itemId": None}, positionals=("itemId",))
        assert out == ("items", "get")

    def test_no_positionals_falls_back_to_flag(self) -> None:
        """Backwards compatibility: default behavior when catalog has no
        positionals info is to emit everything as flags. (camelCase keys
        pass through verbatim — args_to_argv only kebab-cases underscores.)"""
        out = tool_argv("items.get", {"itemId": "x"})
        assert out == ("items", "get", "--itemId=x")

    def test_positional_does_not_mutate_caller_args(self) -> None:
        args = {"itemId": "x", "agent": True}
        tool_argv("items.get", args, positionals=("itemId",))
        assert args == {"itemId": "x", "agent": True}
