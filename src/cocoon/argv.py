"""Translate a (tool, args) pair into argv for the underlying CLI.

printing-press CLIs are cobra-style: tool names like `issues.create` are
nested subcommands invoked as `cli issues create --flag=value`. We split
the dotted tool on `.` to produce the subcommand chain, then render args:

- bool True  -> bare flag (`--verbose`)
- bool False -> dropped (cobra convention; opposite-flags require explicit naming)
- None       -> dropped
- dict/list  -> `--flag=<json>`
- everything else -> `--flag=<str(value)>`

Keys are kebab-cased: `team_id` -> `--team-id`.

When the catalog tells us a tool takes positional args (e.g. `items <itemId>`),
we pull those keys out of `args` first and emit them as bare argv elements in
declared order, before any flags. Without this, `items.get` with
`args={"itemId": "12345"}` would emit `items get --item-id=12345` (a flag
cobra doesn't know about) instead of `items get 12345` (the positional).
"""

import json
from typing import Any


def tool_argv(
    tool: str,
    args: dict[str, Any] | None = None,
    positionals: tuple[str, ...] = (),
    argv_path: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """Render the full argv tail (subcommands + positionals + flags) for one
    tool call.

    `argv_path` is the catalog's actual cobra invocation chain. When
    provided, it overrides splitting `tool` on `.` — necessary because
    pp:endpoint names like `items.get` can carry verb suffixes that don't
    map to real cobra subcommands (real invocation: `items <itemId>`).
    When empty (capability not in catalog), falls back to dot-split.

    `positionals` is the catalog's declared list of positional arg names
    for this tool, in order; matching keys in `args` get emitted as bare
    argv elements and removed from the flag pass."""
    chain = argv_path if argv_path else tuple(part for part in tool.split(".") if part)
    remaining = dict(args or {})
    pos_argv = []
    for name in positionals:
        if name in remaining:
            value = remaining.pop(name)
            if value is None:
                continue
            pos_argv.append(str(value))
    return chain + tuple(pos_argv) + args_to_argv(remaining)


def args_to_argv(args: dict[str, Any]) -> tuple[str, ...]:
    """Render just the --flag portion. Kept separate so server.py / tests
    can construct argv from pieces without going through a tool name."""
    out: list[str] = []
    for key, value in args.items():
        flag = "--" + key.replace("_", "-")
        if value is None or value is False:
            continue
        if value is True:
            out.append(flag)
        elif isinstance(value, (dict, list)):
            out.append(f"{flag}={json.dumps(value, separators=(',', ':'))}")
        else:
            out.append(f"{flag}={value}")
    return tuple(out)
