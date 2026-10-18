"""The setup sections, in wizard order: model, terminal, tools, agent, gateway.

Each section reads the settings as they are on disk, asks its questions through the shared
prompts (so back navigation works) and saves its own part through the config writer when it
ends; a cancel inside a later section therefore leaves every earlier section saved and every
later one untouched.

Modes: ``full`` (first install, every capability offered on), ``blank`` (first install, only the
minimum on: the model, local commands, the terminal and file tools), ``reconfigure`` (an existing
install: every question defaults to the current value).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol

from indus.cli.model_picker import PickerDeps, apply, pick
from indus.cli.prompts import Interactive, ask_checklist, ask_choice, ask_text

if TYPE_CHECKING:
    from indus.foundation.scope import Scope

__all__ = [
    "BLANK_TOOLSETS",
    "SECTIONS",
    "Mode",
    "PlatformToolsModel",
    "SetupContext",
    "ToolsModel",
    "agent_section",
    "gateway_section",
    "model_section",
    "terminal_section",
    "tools_section",
]

type Mode = Literal["full", "blank", "reconfigure"]

PLATFORM = "cli"
BLANK_TOOLSETS = ("terminal", "file")
PLANNED_BACKENDS = ("docker", "ssh")
APPROVAL_MODES = (
    ("manual", "Ask me before every risky command (manual)"),
    ("smart", "Let a model judge first and ask me only when unsure (smart)"),
)


class ToolsModel(Protocol):
    """What the tools section needs: the rows, which are on now, and a way to change them."""

    def choices(self, scope: Scope) -> list[tuple[str, str]]: ...

    def enabled(self, scope: Scope) -> list[str]: ...

    def save(self, scope: Scope, names: list[str]) -> None: ...


class PlatformToolsModel:
    """``indus tools``' model (P07-17) for the ``cli`` platform: the same rows, and a change goes
    through its enable/disable rules into ``platform_toolsets.cli``."""

    def choices(self, scope: Scope) -> list[tuple[str, str]]:
        from indus.cli.commands import tools

        return [(row.name, row.description) for row in tools.list_rows(scope, PLATFORM)]

    def enabled(self, scope: Scope) -> list[str]:
        from indus.cli.commands import tools

        return [row.name for row in tools.list_rows(scope, PLATFORM) if row.enabled]

    def save(self, scope: Scope, names: list[str]) -> None:
        from indus.cli.commands import tools

        on = self.enabled(scope)
        removed = [name for name in on if name not in names]
        added = [name for name in names if name not in on]
        if removed:
            tools.disable_names(scope, PLATFORM, removed)
        if added:
            tools.enable_names(scope, PLATFORM, added)


@dataclass
class SetupContext:
    scope: Scope
    io: Interactive
    mode: Mode = "full"
    picker: PickerDeps = field(default_factory=PickerDeps)
    tools: ToolsModel = field(default_factory=PlatformToolsModel)

    def heading(self, title: str) -> None:
        self.io.say("")
        self.io.say(f"== {title} ==")


def _setting(scope: Scope, key: str) -> Any:
    from indus.foundation import config

    return config.load(scope).get(key)


def _save(scope: Scope, data: dict[str, Any]) -> None:
    from indus.foundation import config

    config.save(scope, data)


# -- sections ---------------------------------------------------------------------------------


def model_section(ctx: SetupContext) -> None:
    ctx.heading("Model and provider")
    ctx.io.say("Pick the service that answers, give it a key, and choose a model.")
    selection = pick(ctx.scope, ctx.io, deps=ctx.picker)
    if selection is None:
        ctx.io.say("Model left unchanged.")
        return
    for note in apply(ctx.scope, selection):
        ctx.io.say(note)


def _backends() -> list[str]:
    from indus.backends.registry import default_entries

    return [entry.name for entry in default_entries()]


def terminal_section(ctx: SetupContext) -> None:
    ctx.heading("Terminal backend")
    ready = _backends()
    current = str(_setting(ctx.scope, "terminal.backend") or "local")
    if ctx.mode == "blank":
        ctx.io.say("Commands run on this machine (local).")
        return
    names = [*ready, *(n for n in PLANNED_BACKENDS if n not in ready)]

    def label(name: str) -> str:
        if name not in ready:
            return f"{name} (not available yet; arrives in a later release)"
        return f"{name} (on this machine)" if name == "local" else name

    labels = [label(name) for name in names]
    default = names.index(current) if current in names else 0
    chosen = names[ask_choice(ctx.io, "Where should commands run?", labels, default)]
    if chosen not in ready:
        ctx.io.say(f"The {chosen} backend is not available in this version; commands stay local.")
        chosen = "local" if current not in ready else current
    if chosen != current:
        _save(ctx.scope, {"terminal": {"backend": chosen}})
    ctx.io.say(f"Commands run on: {chosen}.")


def tools_section(ctx: SetupContext) -> None:
    ctx.heading("Tools")
    choices = ctx.tools.choices(ctx.scope)
    names = [name for name, _ in choices]
    on = ctx.tools.enabled(ctx.scope)
    if ctx.mode == "blank":
        start = [n for n in names if n in BLANK_TOOLSETS]
        ctx.io.say("Only the shell and file tools are on; switch on anything else you want.")
    elif ctx.mode == "full":
        start = list(names)
    else:
        start = [n for n in names if n in on]
    rows = [f"{name} — {description}" for name, description in choices]
    checked = ask_checklist(
        ctx.io,
        "Tools the agent may use in the command line:",
        rows,
        [names.index(n) for n in start],
    )
    chosen = [names[i] for i in checked]
    if set(chosen) == set(on):
        ctx.io.say("Tools unchanged.")
        return
    ctx.tools.save(ctx.scope, chosen)
    ctx.io.say("Tools on: " + (", ".join(chosen) if chosen else "none"))


def _turns_problem(value: str) -> str | None:
    if value.isdigit():
        return None
    return "Please enter a whole number (0 means no cap)."


def agent_section(ctx: SetupContext) -> None:
    ctx.heading("Agent defaults")
    turns = int(_setting(ctx.scope, "agent.max_turns") or 0)
    mode = str(_setting(ctx.scope, "approvals.mode") or "manual")
    if ctx.mode == "blank":
        ctx.io.say(f"Kept the built-in defaults: {turns} model calls per turn, approvals {mode}.")
        return
    answer = ask_text(
        ctx.io,
        "Model calls per turn before the agent stops and sums up (0 = no cap)",
        str(turns),
        validate=_turns_problem,
    )
    keys = [key for key, _ in APPROVAL_MODES]
    labels = [label for _, label in APPROVAL_MODES]
    if mode not in keys:
        keys.append(mode)
        labels.append(f"Keep the current setting ({mode})")
    picked = keys[ask_choice(ctx.io, "Risky commands:", labels, keys.index(mode))]
    changes: dict[str, Any] = {}
    if int(answer) != turns:
        changes["agent"] = {"max_turns": int(answer)}
    if picked != mode:
        changes["approvals"] = {"mode": picked}
    if changes:
        _save(ctx.scope, changes)
    ctx.io.say(f"Agent: {answer} model calls per turn; approvals {picked}.")


def gateway_section(ctx: SetupContext) -> None:
    ctx.heading("Messaging gateway")
    ctx.io.say(
        "Chatting through Telegram, Slack and other apps arrives in a later release; "
        "nothing to set up here yet."
    )


SECTIONS: dict[str, tuple[str, Callable[[SetupContext], None]]] = {
    "model": ("Model and provider", model_section),
    "terminal": ("Terminal backend", terminal_section),
    "tools": ("Tools", tools_section),
    "agent": ("Agent defaults", agent_section),
    "gateway": ("Messaging gateway", gateway_section),
}
"""Section name → (title, function), in wizard order."""
