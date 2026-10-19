"""The start banner and the resume recap of the classic REPL (P06-06).

The banner names the model, where commands run, the working folder and how many tools and skills
are loaded. The recap shows where a resumed conversation left off: the last few exchanges, long
texts shortened by the ``display.resume_*`` settings, the final reply in full. Stored text is
cleaned of terminal escape sequences first, so an old message cannot restyle the screen.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from rich import box
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.text import Text

from indus.cli.repl.textutil import clean, shorten

__all__ = [
    "BannerInfo",
    "RecapEntry",
    "RecapSettings",
    "RecapView",
    "banner",
    "collect_recap",
    "recap",
    "recap_panel",
]

KEY_HINT = "Enter sends · Alt+Enter new line · Ctrl+C stops · /help lists commands"


@dataclass(frozen=True)
class BannerInfo:
    """What the start banner shows."""

    version: str
    model: str
    provider: str = ""
    backend: str = "local"
    cwd: Path | str = ""
    tool_count: int = 0
    skill_count: int = 0
    session_title: str = ""
    resumed: bool = False
    project_skills_pending: int = 0
    """Skills in this repo left unloaded until it is trusted (P13-28)."""


def _home_relative(path: Path | str, home: Path | None) -> str:
    text = str(path)
    if home is not None and text:
        try:
            rest = Path(text).relative_to(home)
        except ValueError:
            return text
        return "~" if str(rest) == "." else f"~/{rest}"
    return text


def banner(info: BannerInfo, *, user_home: Path | None = None) -> RenderableType:
    """The start banner as a rich renderable."""
    rows = Text()

    def row(label: str, value: str) -> None:
        rows.append(f"{label:<9}", style="dim")
        rows.append(f"{value}\n")

    model = info.model or "not set"
    row("model", f"{model} ({info.provider})" if info.provider else model)
    row("commands", f"run on the {info.backend} backend")
    row("folder", _home_relative(info.cwd, user_home) or "(unknown)")
    tools = "tool" if info.tool_count == 1 else "tools"
    skills = "skill" if info.skill_count == 1 else "skills"
    row("loaded", f"{info.tool_count} {tools} · {info.skill_count} {skills}")
    if info.project_skills_pending:
        count = info.project_skills_pending
        row(
            "project",
            f"{count} skill{'' if count == 1 else 's'} in this repo not loaded "
            "(indus skills trust-project)",
        )
    if info.session_title:
        row("session", info.session_title)
    rows.append(KEY_HINT, style="dim italic")
    heading = "resumed" if info.resumed else "ready"
    return Panel(
        rows,
        title=Text(f"Indus Agent {info.version}", style="bold"),
        subtitle=Text(heading, style="dim"),
        box=box.ROUNDED,
        border_style="cyan",
        padding=(0, 1),
        expand=False,
    )


# -- recap -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RecapSettings:
    """The ``display.resume_*`` limits."""

    exchanges: int = 10
    max_user_chars: int = 300
    max_assistant_chars: int = 200
    max_assistant_lines: int = 3

    @classmethod
    def from_config(cls, get: Any) -> RecapSettings:
        """Read the limits through ``get(key, default)``; bad values fall back to defaults."""

        def number(key: str, default: int) -> int:
            try:
                value = int(get(f"display.resume_{key}", default))
            except (TypeError, ValueError):
                return default
            return value if value > 0 else default

        return cls(
            exchanges=number("exchanges", cls.exchanges),
            max_user_chars=number("max_user_chars", cls.max_user_chars),
            max_assistant_chars=number("max_assistant_chars", cls.max_assistant_chars),
            max_assistant_lines=number("max_assistant_lines", cls.max_assistant_lines),
        )


type RecapRole = Literal["user", "assistant", "assistant_last"]


@dataclass(frozen=True)
class RecapEntry:
    role: RecapRole
    text: str


@dataclass(frozen=True)
class RecapView:
    entries: tuple[RecapEntry, ...]
    hidden: int
    """How many older messages were left out."""


def _field(row: object, name: str) -> Any:
    if isinstance(row, Mapping):
        return cast(Mapping[str, Any], row).get(name)
    return getattr(row, name, None)


def _text_of(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in cast(list[Any], content):
            if isinstance(part, Mapping):
                text = cast(Mapping[str, Any], part).get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(parts)
    return str(content)


def _cut_assistant(text: str, settings: RecapSettings) -> str:
    lines = text.splitlines()
    if len(lines) > settings.max_assistant_lines:
        text = "\n".join(lines[: settings.max_assistant_lines]) + " …"
    return shorten(text, settings.max_assistant_chars)


def collect_recap(rows: Iterable[object], settings: RecapSettings | None = None) -> RecapView:
    """The recap entries for stored ``rows`` (transcript rows or mappings with ``role`` /
    ``content``): user and assistant text only, shortened, the last exchanges kept."""
    settings = settings or RecapSettings()
    entries: list[RecapEntry] = []
    last_full: str | None = None
    for row in rows:
        if _field(row, "scaffold") or _field(row, "display_kind") == "hidden":
            continue
        role = _field(row, "role")
        text = clean(_text_of(_field(row, "content"))).strip()
        if role == "user" and text:
            entries.append(RecapEntry("user", shorten(text, settings.max_user_chars)))
        elif role == "assistant" and text:  # a tool-call-only row has no text: skipped
            entries.append(RecapEntry("assistant", _cut_assistant(text, settings)))
            last_full = text
    keep = settings.exchanges * 2
    hidden = max(0, len(entries) - keep)
    kept = entries[hidden:]
    for index in range(len(kept) - 1, -1, -1):
        if kept[index].role == "assistant":
            if last_full is not None:
                kept[index] = RecapEntry("assistant_last", last_full)
            break
    return RecapView(tuple(kept), hidden)


def recap_panel(view: RecapView) -> RenderableType | None:
    """The recap as a dim panel, or None when there is nothing to show."""
    if not view.entries:
        return None
    body = Text()
    if view.hidden:
        noun = "message" if view.hidden == 1 else "messages"
        body.append(f"({view.hidden} earlier {noun} not shown)\n", style="dim italic")
    labels = {"user": "you   ", "assistant": "indus ", "assistant_last": "indus "}
    for index, entry in enumerate(view.entries):
        if index:
            body.append("\n")
        last = entry.role == "assistant_last"
        body.append(labels[entry.role], style="bold" if last else "dim bold")
        first, *rest = entry.text.split("\n")
        body.append(first, style="" if last else "dim")
        for line in rest:
            body.append("\n      " + line, style="" if last else "dim")
    return Panel(
        body,
        title=Text("earlier in this conversation", style="dim"),
        title_align="left",
        box=box.ROUNDED,
        border_style="dim",
        padding=(0, 1),
    )


def recap(rows: Iterable[object], settings: RecapSettings | None = None) -> RenderableType:
    """The recap panel for ``rows``; an empty group when there is nothing to recap."""
    panel = recap_panel(collect_recap(rows, settings))
    return panel if panel is not None else Group()
