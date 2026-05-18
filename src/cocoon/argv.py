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
"""

import json
from typing import Any


def tool_argv(tool: str, args: dict[str, Any] | None = None) -> tuple[str, ...]:
    """Render the full argv tail (subcommands + flags) for one tool call."""
    chain = tuple(part for part in tool.split(".") if part)
    return chain + args_to_argv(args or {})


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
