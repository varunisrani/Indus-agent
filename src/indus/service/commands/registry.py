"""The slash-command registry: one ordered table for every surface (P06-08).

The classic CLI dispatches, completes and prints ``/help`` from this table; the TUI bridge, the
gateway, ACP and the desktop catalogue read the same entries later. It lives in the service layer
so none of them imports the CLI. Each surface keeps its own handlers (the classic CLI's are in
``indus.cli.slash``); an entry only describes the command.

- Names and aliases are matched without regard to case, with or without the leading ``/``; an
  alias resolves to its canonical entry, so adding one here updates every surface.
- ``owner`` names the phase that implements the command. Entries owned by a later phase are listed
  already, so a surface can say when a command arrives instead of calling it unknown.
- ``description`` is the English text; ``description_key`` (``slash.<name>``) is its catalogue
  key in ``locales/en.yaml``. ``describe`` prefers a catalogue entry when one is given.
- Completion lists every name and alias once; after a space it offers the command's
  subcommands.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal, get_args

__all__ = [
    "CATEGORIES",
    "CATEGORY_TITLES",
    "REGISTRY",
    "SURFACES",
    "ArgumentMode",
    "BusyPolicy",
    "Category",
    "CommandDef",
    "CommandRegistry",
    "Completion",
    "PrefixMatch",
    "Surface",
    "command_word",
    "looks_like_command",
]

type Category = Literal["session", "config", "tools", "info", "exit"]
type Surface = Literal["cli", "tui", "gateway", "desktop"]
type BusyPolicy = Literal["dispatch", "reject", "interrupt_then_dispatch"]
type ArgumentMode = Literal["options", "text", "mixed"]

CATEGORIES: Final[tuple[Category, ...]] = get_args(Category.__value__)
SURFACES: Final[frozenset[Surface]] = frozenset(get_args(Surface.__value__))
BUSY_POLICIES: Final[frozenset[BusyPolicy]] = frozenset(get_args(BusyPolicy.__value__))
ARGUMENT_MODES: Final[frozenset[ArgumentMode]] = frozenset(get_args(ArgumentMode.__value__))

CATEGORY_TITLES: Final[Mapping[Category, str]] = {
    "session": "Session",
    "config": "Settings",
    "tools": "Tools and skills",
    "info": "Information",
    "exit": "Leaving",
}

EVERYWHERE: Final[frozenset[Surface]] = SURFACES
TERMINAL: Final[frozenset[Surface]] = frozenset({"cli", "tui"})
NOT_GATEWAY: Final[frozenset[Surface]] = frozenset({"cli", "tui", "desktop"})
CHAT_ONLY: Final[frozenset[Surface]] = frozenset({"gateway"})

# Words in an argument hint that mean free text follows the options.
_PROSE_WORDS: Final = ("text", "prompt", "question", "name", "topic", "focus")


@dataclass(frozen=True)
class CommandDef:
    """One slash command. See the module notes."""

    name: str
    description: str
    category: Category
    aliases: tuple[str, ...] = ()
    args_hint: str = ""
    subcommands: tuple[str, ...] = ()
    surfaces: frozenset[Surface] = EVERYWHERE
    busy_policy: BusyPolicy = "reject"
    argument_mode: ArgumentMode | None = None
    owner: str = "P06"

    @property
    def description_key(self) -> str:
        """The English catalogue key of the description."""
        return f"slash.{self.name}"

    @property
    def keys(self) -> tuple[str, ...]:
        """The name, then the aliases."""
        return (self.name, *self.aliases)

    @property
    def mode(self) -> ArgumentMode | None:
        """How a composer treats the argument: explicit, else inferred from the entry."""
        if self.argument_mode is not None:
            return self.argument_mode
        hint = self.args_hint.lower()
        if self.subcommands:
            return "mixed" if any(word in hint for word in _PROSE_WORDS) else "options"
        return "text" if hint else None

    def offered_on(self, surface: Surface | None) -> bool:
        return surface is None or surface in self.surfaces

    def usage(self) -> str:
        return f"/{self.name} {self.args_hint}".rstrip()


def _cmd(
    name: str,
    description: str,
    category: Category,
    *aliases: str,
    args: str = "",
    subs: Sequence[str] = (),
    surfaces: frozenset[Surface] = EVERYWHERE,
    busy: BusyPolicy = "reject",
    mode: ArgumentMode | None = None,
    owner: str = "P06",
) -> CommandDef:
    return CommandDef(
        name=name,
        description=description,
        category=category,
        aliases=tuple(aliases),
        args_hint=args,
        subcommands=tuple(subs),
        surfaces=surfaces,
        busy_policy=busy,
        argument_mode=mode,
        owner=owner,
    )


REASONING_WORDS: Final = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultra",
    "show",
    "hide",
)

BUILTIN: Final[tuple[CommandDef, ...]] = (
    # -- session --------------------------------------------------------------------------------
    _cmd(
        "new",
        "Start a fresh session; add a name to title it, 'now' or -y to skip the question",
        "session",
        "reset",
        args="[name] [now|-y]",
        busy="interrupt_then_dispatch",
    ),
    _cmd("clear", "Clear the screen and start a fresh session", "session", surfaces=TERMINAL),
    _cmd("history", "Show this conversation so far", "session", surfaces=TERMINAL, busy="dispatch"),
    _cmd("title", "Show or set the title of this session", "session", args="[name]"),
    _cmd(
        "resume",
        "Switch to an earlier session by title, id or list number",
        "session",
        args="[name|number]",
        mode="mixed",
    ),
    _cmd("sessions", "List recent sessions to pick one to resume", "session", args="[name|number]"),
    _cmd("retry", "Take back your last message and send it again", "session"),
    _cmd(
        "undo",
        "Take back your last N messages and their replies (default 1)",
        "session",
        args="[N]",
    ),
    _cmd(
        "save",
        "Write this conversation to a file as JSON lines, Markdown or HTML",
        "session",
        args="json|md|html [file]",
        subs=("json", "md", "html"),
        mode="mixed",
    ),
    _cmd(
        "branch",
        "Copy this conversation into a new session and continue there",
        "session",
        "fork",
        args="[name]",
    ),
    _cmd(
        "stop",
        "Stop the running reply and this session's background processes",
        "session",
        busy="interrupt_then_dispatch",
    ),
    _cmd(
        "steer",
        "Add guidance to the running reply after its next tool call",
        "session",
        "s",
        args="<text>",
        busy="dispatch",
    ),
    _cmd(
        "queue",
        "Line up a message for after the current reply; list, clear or remove queued ones",
        "session",
        "q",
        args="<text>|add <text>|list|clear|rm N",
        subs=("add", "list", "clear", "rm"),
        busy="dispatch",
        mode="mixed",
    ),
    _cmd("compress", "Summarise older turns to free context", "session", "compact", args="[focus]"),
    _cmd("status", "Show the session, model and approval state", "session", busy="dispatch"),
    _cmd(
        "context",
        "Show how much of the context window is in use",
        "session",
        "ctx",
        busy="dispatch",
    ),
    _cmd(
        "btw",
        "Ask a side question without changing the conversation",
        "session",
        args="<question>",
        busy="dispatch",
        owner="P10",
    ),
    _cmd(
        "goal",
        "Set a standing goal the agent keeps working towards",
        "session",
        args="[text|status|pause|resume|clear]",
        busy="dispatch",
        mode="mixed",
        owner="P10",
    ),
    _cmd(
        "subgoal",
        "Add or remove extra criteria on the current goal",
        "session",
        args="[text|remove N|clear]",
        busy="dispatch",
        mode="mixed",
        owner="P10",
    ),
    _cmd(
        "loop",
        "Repeat a prompt in this session on an interval",
        "session",
        "proactive",
        args="[interval] <prompt>",
        busy="dispatch",
        mode="mixed",
        owner="P10",
    ),
    _cmd(
        "heartbeat",
        "Send a recurring prompt into this session while it is idle",
        "session",
        "hb",
        args="[every <interval> <prompt>|status|pause|resume|clear]",
        subs=("status", "pause", "resume", "clear"),
        busy="dispatch",
        owner="P10",
    ),
    _cmd(
        "moa",
        "Answer one prompt with several models, then return to yours",
        "session",
        args="<prompt>",
        owner="P10",
    ),
    _cmd(
        "agents",
        "Show running helper agents and background tasks",
        "session",
        "tasks",
        busy="dispatch",
        owner="P10",
    ),
    _cmd(
        "refine",
        "Review this conversation now and keep what was learned",
        "session",
        args="[focus]",
        owner="P09",
    ),
    _cmd(
        "journey",
        "Browse what the agent has learned over time",
        "session",
        "learning",
        args="[list|delete <id>|edit <id>]",
        subs=("list", "delete", "edit"),
        surfaces=TERMINAL,
        owner="P09",
    ),
    _cmd(
        "rollback",
        "List or restore file checkpoints",
        "session",
        args="[N [file]|diff N|--all]",
        subs=("diff",),
        mode="options",
        owner="P11",
    ),
    _cmd(
        "worktree",
        "Create, list or prune isolated git worktrees",
        "session",
        args="[list|new [name]|prune [--dry-run]]",
        subs=("list", "new", "prune"),
        surfaces=frozenset({"cli"}),
        owner="P11",
    ),
    _cmd(
        "approve",
        "Approve the command waiting for an answer",
        "session",
        args="[session|always]",
        surfaces=CHAT_ONLY,
        busy="dispatch",
        owner="P15",
    ),
    _cmd(
        "deny",
        "Refuse the command waiting for an answer",
        "session",
        args="[reason]",
        surfaces=CHAT_ONLY,
        busy="dispatch",
        owner="P15",
    ),
    # -- settings -------------------------------------------------------------------------------
    _cmd(
        "model",
        "Show or switch the model; --global also saves it",
        "config",
        args="[provider:model] [--provider name] [--global]",
    ),
    _cmd(
        "reasoning",
        "Show or set reasoning effort, or show/hide reasoning text",
        "config",
        args="[level|show|hide] [--global]",
        subs=REASONING_WORDS,
    ),
    _cmd(
        "yolo",
        "Turn auto-approval of risky commands on or off for this session",
        "config",
        busy="dispatch",
    ),
    _cmd(
        "approvals",
        "Show or save how risky commands are approved",
        "config",
        args="[manual|smart|off]",
        subs=("manual", "smart", "off"),
    ),
    _cmd(
        "fast",
        "Ask the provider for faster processing",
        "config",
        args="[normal|fast|auto|status] [--global]",
        subs=("normal", "fast", "auto", "status"),
        owner="P08",
    ),
    _cmd(
        "config",
        "Show the effective settings with secrets masked",
        "config",
        surfaces=TERMINAL,
        busy="dispatch",
    ),
    _cmd(
        "voice",
        "Turn voice input and spoken replies on or off",
        "config",
        args="[on|off|status]",
        subs=("on", "off", "status"),
        owner="P10",
    ),
    # -- tools and skills -----------------------------------------------------------------------
    _cmd(
        "tools",
        "List tools, or enable/disable toolsets (starts a fresh session)",
        "tools",
        args="[list|enable|disable] [name...]",
        subs=("list", "enable", "disable"),
        surfaces=TERMINAL,
        mode="mixed",
    ),
    _cmd(
        "skills",
        "Find, install and manage skills",
        "tools",
        args="[list|view|reload|pending|approve|reject] [name|id]",
        subs=("list", "view", "reload", "pending", "approve", "reject"),
        mode="mixed",
        owner="P09",
    ),
    _cmd(
        "reload-skills",
        "Rescan skill folders and refresh skill commands (the prompt stays as it is)",
        "tools",
        owner="P09",
    ),
    _cmd("bundles", "List skill bundles (each runs as its own /command)", "tools", owner="P09"),
    _cmd(
        "memory",
        "Review memory writes waiting for approval",
        "tools",
        args="[pending|approve|reject|approval|status] [id|all|on|off]",
        subs=("pending", "approve", "reject", "approval", "status"),
        mode="mixed",
        owner="P09",
    ),
    _cmd(
        "learn",
        "Turn something you describe into a reusable skill",
        "tools",
        args="<what to learn from>",
        owner="P09",
    ),
    _cmd(
        "curator",
        "Background upkeep of skills",
        "tools",
        args="[status|run|pause|resume]",
        subs=("status", "run", "pause", "resume"),
        owner="P09",
    ),
    _cmd(
        "browser",
        "Connect the browser tools to a running browser",
        "tools",
        args="[connect|disconnect|status]",
        subs=("connect", "disconnect", "status"),
        surfaces=TERMINAL,
        owner="P10",
    ),
    _cmd(
        "checkpoints",
        "Turn file checkpoints on or off",
        "tools",
        args="[on|off|status]",
        subs=("on", "off", "status"),
        owner="P11",
    ),
    _cmd("mcp", "Show connected MCP servers", "tools", owner="P13"),
    _cmd("reload-mcp", "Reconnect MCP servers from the config", "tools", "reload_mcp", owner="P13"),
    _cmd("plugins", "List installed plugins", "tools", surfaces=TERMINAL, owner="P13"),
    _cmd(
        "cron",
        "Manage scheduled jobs",
        "tools",
        args="[subcommand]",
        surfaces=TERMINAL,
        owner="P14",
    ),
    # -- information ----------------------------------------------------------------------------
    _cmd(
        "help", "List commands; add a word to filter them", "info", args="[filter]", busy="dispatch"
    ),
    _cmd("usage", "Show token use and cost of this session", "info", busy="dispatch"),
    _cmd("whoami", "Show the profile, home and session you are on", "info", busy="dispatch"),
    _cmd("version", "Show the Indus Agent version", "info", "v", busy="dispatch"),
    # -- leaving --------------------------------------------------------------------------------
    _cmd("quit", "Leave Indus Agent", "exit", "exit", surfaces=TERMINAL, busy="dispatch"),
)


@dataclass(frozen=True)
class Completion:
    """One completion candidate: the text that replaces the typed word, and a short note."""

    text: str
    meta: str = ""


@dataclass(frozen=True)
class PrefixMatch:
    """How a typed word matched a set of names."""

    kind: Literal["exact", "unique", "ambiguous", "none"]
    names: tuple[str, ...] = ()


def command_word(text: str) -> tuple[str, str]:
    """``(first word without the slash, lowercased; the rest, case kept and stripped)``."""
    stripped = text.strip()
    head, _, rest = stripped.partition(" ")
    return head.lstrip("/").lower(), rest.strip()


def looks_like_command(text: str) -> bool:
    """``/help`` is a command; ``/usr/bin/env`` (a path) and a lone ``/`` are not."""
    stripped = text.strip()
    if not stripped.startswith("/") or len(stripped) < 2:
        return False
    first = stripped.split(maxsplit=1)[0]
    return "/" not in first[1:]


class CommandRegistry:
    """An ordered, validated command table with lookup, grouping and completion."""

    def __init__(self, commands: Iterable[CommandDef]) -> None:
        self._commands: tuple[CommandDef, ...] = tuple(commands)
        self._lookup: dict[str, CommandDef] = {}
        for cmd in self._commands:
            self._check(cmd)
            for key in cmd.keys:
                if key in self._lookup:
                    raise ValueError(f"slash command name /{key} is used twice")
                self._lookup[key] = cmd

    @staticmethod
    def _check(cmd: CommandDef) -> None:
        for key in cmd.keys:
            if not key or key != key.lower() or key.strip() != key or " " in key or "/" in key:
                raise ValueError(f"bad slash command name {key!r}")
        if cmd.category not in CATEGORIES:
            raise ValueError(f"/{cmd.name}: unknown category {cmd.category!r}")
        if cmd.busy_policy not in BUSY_POLICIES:
            raise ValueError(f"/{cmd.name}: unknown busy policy {cmd.busy_policy!r}")
        if cmd.argument_mode is not None and cmd.argument_mode not in ARGUMENT_MODES:
            raise ValueError(f"/{cmd.name}: unknown argument mode {cmd.argument_mode!r}")
        if not cmd.surfaces or not cmd.surfaces <= SURFACES:
            raise ValueError(f"/{cmd.name}: bad surfaces {sorted(cmd.surfaces)}")
        if not cmd.description.strip():
            raise ValueError(f"/{cmd.name}: no description")

    # -- lookup ---------------------------------------------------------------------------------

    def __iter__(self) -> Iterator[CommandDef]:
        return iter(self._commands)

    def __len__(self) -> int:
        return len(self._commands)

    def resolve(self, word: str) -> CommandDef | None:
        """The entry a name or alias names (case-insensitive, leading ``/`` optional)."""
        return self._lookup.get(word.strip().lstrip("/").lower())

    def commands(self, surface: Surface | None = None) -> list[CommandDef]:
        """Entries offered on ``surface`` (all for None), in table order."""
        return [cmd for cmd in self._commands if cmd.offered_on(surface)]

    def keys(self, surface: Surface | None = None) -> list[str]:
        """Every name and alias offered on ``surface``, each once."""
        return [key for cmd in self.commands(surface) for key in cmd.keys]

    def by_category(
        self, commands: Iterable[CommandDef] | None = None
    ) -> dict[Category, list[CommandDef]]:
        """``commands`` (default: all) grouped by category, in category then table order;
        empty categories are left out."""
        chosen = list(self._commands if commands is None else commands)
        grouped: dict[Category, list[CommandDef]] = {}
        for category in CATEGORIES:
            members = [cmd for cmd in chosen if cmd.category == category]
            if members:
                grouped[category] = members
        return grouped

    @staticmethod
    def describe(cmd: CommandDef, catalogue: Mapping[str, str] | None = None) -> str:
        """The catalogue text for the command when present, else the English fallback."""
        if catalogue is not None:
            text = catalogue.get(cmd.description_key)
            if text:
                return text
        return cmd.description

    # -- prefixes and completion ----------------------------------------------------------------

    @staticmethod
    def prefix_matches(word: str, names: Iterable[str]) -> PrefixMatch:
        """How ``word`` matches ``names``: exact, one prefix, several, or none."""
        wanted = word.strip().lstrip("/").lower()
        pool = sorted(set(names))
        if not wanted:
            return PrefixMatch("none")
        if wanted in pool:
            return PrefixMatch("exact", (wanted,))
        hits = tuple(name for name in pool if name.startswith(wanted))
        if len(hits) == 1:
            return PrefixMatch("unique", hits)
        if hits:
            return PrefixMatch("ambiguous", hits)
        return PrefixMatch("none")

    def complete(
        self, text: str, names: Iterable[str], *, extra: Mapping[str, str] | None = None
    ) -> list[Completion]:
        """Candidates for the slash text typed so far.

        ``names`` are the command names and aliases this surface can run; ``extra`` maps further
        runnable names (quick, plugin, skill commands) to a note. While the first word is being
        typed every matching name is offered once, sorted; after a space the command's
        subcommands that match the second word are offered.
        """
        if not text.startswith("/"):
            return []
        body = text[1:]
        notes: dict[str, str] = {}
        for name in names:
            cmd = self.resolve(name)
            if cmd is not None:
                notes[name] = cmd.description if name == cmd.name else f"same as /{cmd.name}"
        for name, note in (extra or {}).items():
            notes.setdefault(name.lower(), note)
        if " " not in body:
            typed = body.lower()
            return [
                Completion(f"/{name}", notes[name])
                for name in sorted(notes)
                if name.startswith(typed)
            ]
        head, _, rest = body.partition(" ")
        cmd = self.resolve(head)
        if cmd is None or head.lower() not in notes or " " in rest.strip():
            return []
        typed = rest.strip().lower()
        return [Completion(sub) for sub in cmd.subcommands if sub.startswith(typed)]


REGISTRY: Final = CommandRegistry(BUILTIN)
"""The built-in commands."""
