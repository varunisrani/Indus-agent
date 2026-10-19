"""The ``indus`` click app: global flags, the command list, and argv clean-up before parsing.

``REGISTRY`` is the one ordered list of top-level commands (``name -> "module:attr"``); its order
is the ``--help`` order. Commands are imported only when used (``LazyGroup``).

``rewrite_session_flags`` runs before click: ``-c/--continue`` takes an optional session name
that may be several words long, so the words after it are joined into one value and the flag is
always handed to click as ``--continue=<value>`` (a bare ``-c`` becomes ``--continue=__latest__``).
The run of words after the flag ends at the next flag, or at a word naming a command when at least
one name word came before it; a command word right after ``-c`` is the command (``indus -c chat``)
unless another command word follows (``indus -c model chat`` resumes "model"). ``-r/--resume``
joins its words the same way but always takes the first one. ``-w/--worktree`` takes one optional
name: the next word unless it is a flag or a command (``indus -w chat`` starts chat in an unnamed
worktree); click always gets ``--worktree=<name>`` (``__auto__`` for none).

``chat`` repeats most global flags; their copies default to "not given" so a value placed before
the subcommand is never overwritten by the subcommand's default (R04.21).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, cast

import click

from indus.cli.args import (
    AUTO_WORKTREE,
    BARE_CONTINUE,
    CHAT_VALUE_FLAGS,
    ChatArgs,
    CliState,
    GlobalArgs,
)
from indus.cli.lazy import LazyGroup

__all__ = [
    "HIDDEN",
    "REGISTRY",
    "cli",
    "dispatch",
    "global_options",
    "globals_from",
    "rewrite_session_flags",
    "value_flag_sets",
    "with_global_options",
]

REGISTRY: tuple[tuple[str, str, str], ...] = (
    ("chat", "indus.cli.chat:chat_command", "Chat with the agent (the default command)."),
    ("model", "indus.cli.model_picker:model_command", "Choose the provider and model for chats."),
    (
        "fallback",
        "indus.cli.commands.fallback:fallback_command",
        "Routes tried in order when the main model fails.",
    ),
    ("setup", "indus.cli.setup:setup_command", "Set up Indus Agent step by step."),
    ("auth", "indus.cli.commands.auth:auth_group", "Manage provider keys, sign-ins and pools."),
    ("status", "indus.cli.commands.status:status_command", "Show model, provider and store."),
    ("doctor", "indus.cli.commands.doctor:doctor_command", "Check the setup; --fix repairs."),
    (
        "approvals",
        "indus.cli.commands.approvals:approvals_command",
        "Check what the approval rules would do with a command.",
    ),
    ("dump", "indus.cli.commands.dump:dump_command", "Print a setup summary for support."),
    ("backup", "indus.cli.commands.backup:backup_command", "Back up the home into a zip file."),
    (
        "checkpoints",
        "indus.cli.commands.checkpoints:checkpoints_group",
        "Inspect and trim file checkpoints.",
    ),
    (
        "worktree",
        "indus.cli.commands.worktree:worktree_group",
        "Create, list and prune session worktrees.",
    ),
    ("import", "indus.cli.commands.import_cmd:import_command", "Restore a backup zip."),
    ("config", "indus.cli.commands.config_read:config_command", "Show and change settings."),
    (
        "tools",
        "indus.cli.commands.tools:tools_command",
        "Choose which toolsets each platform gets.",
    ),
    (
        "sessions",
        "indus.cli.commands.sessions:sessions_group",
        "List, export and tidy saved sessions.",
    ),
    (
        "profile",
        "indus.cli.commands.profile:profile_command",
        "Create, switch and manage profiles.",
    ),
    (
        "completion",
        "indus.cli.commands.completion:completion_command",
        "Print a shell completion script (bash, zsh or fish).",
    ),
    ("logs", "indus.cli.commands.logs:logs_command", "Show and follow the log files."),
    ("bundles", "indus.cli.commands.bundles:bundles_group", "Group skills into one slash command."),
    ("skills", "indus.cli.commands.skills:skills_group", "List, check and sync skills."),
    (
        "memory",
        "indus.cli.commands.memory:memory_group",
        "See, edit and approve what the agent remembers.",
    ),
    (
        "curator",
        "indus.cli.commands.curator:curator_group",
        "Look after the skills the agent wrote for itself.",
    ),
    ("journey", "indus.cli.commands.journey:journey_group", "What the agent has learned."),
    ("learning", "indus.cli.commands.journey:journey_group", "Same as `indus journey`."),
    ("moa", "indus.cli.commands.moa:moa_group", "Mixture-of-agents presets for /moa."),
)
"""Top-level commands in ``--help`` order: ``(name, "module:attr", one-line help)``. Later phases
append in the order of research 04 §3.2.4."""

HIDDEN: tuple[tuple[str, str], ...] = (
    ("tui-bridge", "indus.tui_bridge.command:tui_bridge_command"),
)
"""Commands other programs start (never listed): ``(name, "module:attr")``."""

_CONTINUE = ("-c", "--continue")
_RESUME = ("-r", "--resume")
_WORKTREE = ("-w", "--worktree")

type Decorator = Callable[[Callable[..., Any]], Callable[..., Any]]


def _complete_with(name: str) -> Callable[[click.Context, click.Parameter, str], list[Any]]:
    """A ``shell_complete`` hook that imports the completion module only when a shell asks."""

    def complete(ctx: click.Context, param: click.Parameter, incomplete: str) -> list[Any]:
        from indus.cli.commands import completion

        hook: Callable[[click.Context, click.Parameter, str], list[Any]] = getattr(completion, name)
        return hook(ctx, param, incomplete)

    return complete


def _not_given(flag: bool) -> dict[str, Any]:
    return {"default": None} if not flag else {"is_flag": True, "default": False}


def global_options(*, repeated: bool = False) -> list[Decorator]:
    """The global flags as click options. ``repeated`` gives ``chat``'s copies (no ``-z``,
    ``--usage-file``, ``-V``, ``-p``)."""
    opts: list[Decorator] = []
    add = opts.append
    if not repeated:
        add(
            click.option(
                "-p",
                "--profile",
                metavar="NAME",
                expose_value=False,
                shell_complete=_complete_with("complete_profiles"),
                help="Run against the named profile (works in any position).",
            )
        )
        add(
            click.option(
                "-z",
                "--oneshot",
                metavar="PROMPT",
                default=None,
                help="Answer one prompt and print only the final reply (for scripts).",
            )
        )
        add(
            click.option(
                "--usage-file",
                metavar="PATH",
                default=None,
                help="With -z: write a JSON usage report to PATH, even when the run fails.",
            )
        )
    add(click.option("-m", "--model", default=None, help="Model for this run."))
    add(click.option("--provider", default=None, help="Provider for this run."))
    add(
        click.option(
            "--reasoning",
            metavar="LEVEL",
            default=None,
            help="Reasoning effort: none, minimal, low, medium, high, xhigh, max or ultra.",
        )
    )
    add(
        click.option(
            "-t",
            "--toolsets",
            default=None,
            shell_complete=_complete_with("complete_toolsets"),
            help="Comma-separated toolsets to enable.",
        )
    )
    add(
        click.option(
            "-r",
            "--resume",
            metavar="SESSION",
            default=None,
            shell_complete=_complete_with("complete_session_titles"),
            help="Resume a session by id or title; 'latest' picks the newest one here.",
        )
    )
    add(
        click.option(
            "-c",
            "--continue",
            "continue_",
            metavar="[NAME]",
            is_flag=False,
            flag_value=BARE_CONTINUE,
            default=None,
            shell_complete=_complete_with("complete_session_titles"),
            help="Continue the session called NAME, or the newest one when no name is given.",
        )
    )
    add(
        click.option(
            "--in",
            "in_dir",
            metavar="DIR",
            default=None,
            help="Work in DIR: change into it first and keep the session there.",
        )
    )
    add(
        click.option(
            "--no-restore-cwd",
            **_not_given(True),
            help="Stay in the current directory when resuming.",
        )
    )
    add(
        click.option(
            "-s",
            "--skills",
            multiple=True,
            default=None,
            help="Skills to preload (repeat the flag or separate with commas).",
        )
    )
    add(
        click.option(
            "-w",
            "--worktree",
            metavar="[NAME]",
            default=None,
            help="Start the session in a new git worktree of this repository (named NAME).",
        )
    )
    add(
        click.option(
            "--checkpoints",
            **_not_given(True),
            help="Snapshot the working folder before file changes (/rollback restores).",
        )
    )
    add(click.option("--yolo", **_not_given(True), help="Run risky commands without asking."))
    add(
        click.option(
            "--pass-session-id",
            **_not_given(True),
            help="Tell the model its session id.",
        )
    )
    add(
        click.option(
            "--ignore-user-config",
            **_not_given(True),
            help="Use built-in settings instead of config.yaml (.env still loads).",
        )
    )
    add(
        click.option(
            "--ignore-rules",
            **_not_given(True),
            help="Skip project rules files, memory and preloaded skills.",
        )
    )
    add(
        click.option(
            "--safe-mode",
            **_not_given(True),
            help="Troubleshoot with every customisation off (implies both --ignore flags).",
        )
    )
    add(
        click.option(
            "--tui/--cli",
            "interface",
            default=None,
            help="Pick the full-screen interface or the classic prompt.",
        )
    )
    return opts


def with_global_options(*, repeated: bool = False) -> Decorator:
    """One decorator adding every option of ``global_options(repeated=...)`` in order."""

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        for option in reversed(global_options(repeated=repeated)):
            fn = option(fn)
        return fn

    return decorate


def _interface_word(value: object) -> str | None:
    if value is True:
        return "tui"
    if value is False:
        return "cli"
    return None


def globals_from(values: dict[str, Any]) -> GlobalArgs:
    """``GlobalArgs`` from click's parameter values (``interface`` as a word)."""
    values = dict(values)
    values["interface"] = _interface_word(values.get("interface"))
    for name in ("no_restore_cwd", "yolo", "pass_session_id", "checkpoints"):
        if values.get(name) is False:
            values.pop(name)
    if not values.get("skills"):
        values.pop("skills", None)
    return GlobalArgs.from_values(values)


def _show_version(ctx: click.Context, _param: click.Parameter, value: bool) -> None:
    if not value or ctx.resilient_parsing:
        return
    from indus.cli.version import version_lines

    for line in version_lines():
        click.echo(line)
    ctx.exit(0)


@click.group(
    cls=LazyGroup,
    name="indus",
    registry=REGISTRY,
    hidden=HIDDEN,
    invoke_without_command=True,
    context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 100},
    help="Indus Agent: a terminal agent that can run commands, edit files and search the web."
    "\n\nRun `indus` with no command to start a chat.",
)
@click.option(
    "-V",
    "--version",
    is_flag=True,
    expose_value=False,
    is_eager=True,
    callback=_show_version,
    help="Show the version and how Indus Agent was installed.",
)
@with_global_options()
@click.pass_context
def cli(ctx: click.Context, **values: Any) -> int:
    """The group callback: record the global flags; with no subcommand run -z or chat."""
    state = ctx.find_object(CliState)
    if state is None:
        raise click.UsageError("the indus app was started without its process state")
    state.globals = globals_from(values)
    if ctx.invoked_subcommand is not None:
        return 0
    if state.globals.oneshot is not None:
        from indus.cli.oneshot import run_from_cli

        return run_from_cli(state)
    from indus.cli.chat import run_chat

    return run_chat(state, state.globals, ChatArgs())


def _option_flags(params: Sequence[click.Parameter]) -> tuple[set[str], set[str]]:
    required: set[str] = set()
    optional: set[str] = set()
    for param in params:
        if not isinstance(param, click.Option) or param.is_flag or param.count:
            continue
        names = {*param.opts, *param.secondary_opts}
        if param.is_bool_flag:
            continue
        if getattr(param, "_flag_needs_value", False):
            optional |= names
        else:
            required |= names
    return required, optional


def value_flag_sets() -> tuple[frozenset[str], frozenset[str]]:
    """``(required-value, optional-value)`` top-level flags, read from the live app."""
    required, optional = _option_flags(cast(LazyGroup, cli).params)
    return frozenset(required), frozenset(optional)


def _value_flags_all() -> frozenset[str]:
    required, _optional = value_flag_sets()
    return required | CHAT_VALUE_FLAGS


def rewrite_session_flags(argv: Sequence[str], commands: Sequence[str] | None = None) -> list[str]:
    """Join multi-word session names after ``-c``/``-r`` (see the module notes)."""
    known = set(commands if commands is not None else cast(LazyGroup, cli).registered)
    value_flags = _value_flags_all() - {*_RESUME, *_WORKTREE}
    out: list[str] = []
    i = 0
    n = len(argv)
    while i < n:
        token = argv[i]
        if token == "--":
            out.extend(argv[i:])
            break
        if token in _WORKTREE:
            following = argv[i + 1] if i + 1 < n else None
            if following is None or following.startswith("-") or following in known:
                out.append(f"--worktree={AUTO_WORKTREE}")
                i += 1
            else:
                out.append(f"--worktree={following}")
                i += 2
            continue
        if token in _CONTINUE or token in _RESUME:
            j = i + 1
            words: list[str] = []
            while j < n and not argv[j].startswith("-"):
                word = argv[j]
                if word in known:
                    if words:
                        break
                    later = argv[j + 1 :]
                    another = any(w in known for w in later if not w.startswith("-"))
                    if token in _CONTINUE and not another:
                        break
                words.append(word)
                j += 1
            if token in _CONTINUE:
                out.append(f"--continue={' '.join(words) if words else BARE_CONTINUE}")
            elif words:
                out.append(f"--resume={' '.join(words)}")
            else:
                out.append(token)
            i = j
            continue
        out.append(token)
        if token in value_flags and i + 1 < n:
            out.append(argv[i + 1])
            i += 2
            continue
        i += 1
    return out


def dispatch(argv: Sequence[str], state: CliState) -> int:
    """Parse ``argv`` and run the command; returns the exit code (usage errors are 2)."""
    args = rewrite_session_flags(argv)
    try:
        result = cli.main(args=args, prog_name="indus", obj=state, standalone_mode=False)
    except click.exceptions.Abort:
        click.echo("Stopped.", err=True)
        return 130
    except click.ClickException as exc:
        exc.show()
        return exc.exit_code
    if isinstance(result, int) and not isinstance(result, bool):
        return result
    return 0
