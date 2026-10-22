"""Job blueprints: a fixed recurrence with slots for the human parts (P24-46).

A blueprint is a job template. Its ``schedule`` is fixed except for the placeholders
``{minute}`` and ``{hour}`` (filled from a ``time`` slot), ``{dow}`` (from a ``weekdays`` slot)
and ``{interval}`` (from an ``interval`` slot); its ``prompt`` may name any slot as ``{name}``.
Slot kinds and what they accept:

- ``time``: ``HH:MM`` on a 24-hour clock (``7:05`` and ``07:05`` both work);
- ``weekdays``: ``daily``, ``weekdays``, ``weekends``, or days as names (``mon``, ``Tuesday``) or
  numbers 0–6 (0 is Sunday) joined by commas;
- ``choice``: one of the slot's ``options`` (any case);
- ``interval``: whole minutes between the slot's ``min`` and ``max``;
- ``text``: one line, at most ``max`` characters (default 200).

A value outside its slot's schema, a slot the blueprint does not have, or a required slot left
empty is refused with ``BlueprintError``; a missing slot takes its default. ``fill`` gives the job
spec ``{name, schedule, prompt, deliver}`` that the normal job creation takes; the rendered
schedule is checked with the schedule grammar and must recur.

Blueprints come from the starter catalogue below (ours) and from skills: ``SKILL.md`` frontmatter
under ``<home>/skills/`` may carry ``metadata.indus.blueprints`` (a list) or ``blueprint`` (one
mapping) with ``key``, ``title``, ``description``, ``schedule``, ``prompt``, ``slots`` and the
optional ``deliver`` and ``integrations``; its key is shown as ``<skill>:<key>``. A skill
blueprint that does not load (a placeholder other than the four, a placeholder with no slot to
fill it, a bad slot) is skipped with a warning and never offered. The cron package sits beside
the skills package (layer rule), so the frontmatter is read here directly.

``integrations`` names what a blueprint is waiting for (wave 45): when an integration is connected
(``service.integration_offers``), each blueprint whose list holds the integration's kind
(``platform``: any messaging platform; ``mcp``: any MCP server), its bare name (``telegram``,
``github``) or ``kind:name`` is offered as a suggested job, when it fills from its defaults. Names
are short lowercase words (``_INTEGRATION``); the starter morning briefing waits for ``platform``.

One blueprint renders three ways: ``form`` (fields for the dashboard and desktop), ``cron_line``
(a ``/cron add`` line pre-filled from the defaults or given values) and ``seed`` (a prompt that
has the agent ask for each slot and then create the job with the cron tool).

``handle_blueprint`` is the ``/blueprint`` command for every surface: no argument lists the
blueprints; ``<name>`` alone returns a reply with ``seed`` set (the surface hands it to the agent
as the next user turn); ``<name> slot=value …`` creates the job at once. Names match exactly
first, then by the start of the key (or of the part after ``:``) or of a title word, then
anywhere in key, title or description, then a close spelling of a key; several matches are
listed, and no match suggests the nearest keys.
"""

from __future__ import annotations

import difflib
import logging
import re
import shlex
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from indus.cron import ports, targets
from indus.cron.errors import CronStoreBusy, ScheduleError
from indus.cron.grammar import is_recurring, parse_schedule
from indus.foundation import yamlio
from indus.foundation.scope import Scope

__all__ = [
    "CATALOGUE",
    "SCHEDULE_FIELDS",
    "SLOT_KINDS",
    "WEEKDAY_PRESETS",
    "Blueprint",
    "BlueprintError",
    "BlueprintReply",
    "Slot",
    "blueprint_from_mapping",
    "cron_line",
    "fill",
    "form",
    "handle_blueprint",
    "list_blueprints",
    "match_blueprint",
    "seed",
    "skill_blueprints",
]

log = logging.getLogger(__name__)

type SlotKind = Literal["time", "weekdays", "choice", "interval", "text"]
SLOT_KINDS: frozenset[str] = frozenset({"time", "weekdays", "choice", "interval", "text"})

SCHEDULE_FIELDS: Mapping[str, str] = {
    "minute": "time",
    "hour": "time",
    "dow": "weekdays",
    "interval": "interval",
}
"""Schedule placeholder → the slot kind that fills it."""

WEEKDAY_PRESETS: Mapping[str, str] = {"daily": "*", "weekdays": "1-5", "weekends": "0,6"}
_DAY_NAMES = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")
_DAY_WORDS = {
    **{name: index for index, name in enumerate(_DAY_NAMES)},
    **{
        full: index
        for index, full in enumerate(
            ("sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday")
        )
    },
    "tues": 2,
    "wednes": 3,
    "thur": 4,
    "thurs": 4,
}
_TIME = re.compile(r"^(\d{1,2}):(\d{2})$")
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_KEY = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SLOT_NAME = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_INTEGRATION = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}(:[a-z0-9][a-z0-9._-]{0,63})?$")
MAX_INTEGRATIONS = 16
TEXT_MAX = 200
INTERVAL_MIN = 5
INTERVAL_MAX = 7 * 24 * 60
SKILL_FILE = "SKILL.md"
MAX_SKILL_FILES = 2000


class BlueprintError(ValueError):
    """A blueprint that does not load, or a value its slot refuses (the message says which)."""


@dataclass(frozen=True)
class Slot:
    """One human part of a blueprint (see the module notes)."""

    name: str
    kind: SlotKind
    label: str
    default: str | None = None
    options: tuple[str, ...] = ()
    optional: bool = False
    help: str = ""
    minimum: int = 0
    """Interval slots: fewest minutes (0 = ``INTERVAL_MIN``)."""
    maximum: int = 0
    """Interval slots: most minutes (0 = ``INTERVAL_MAX``); text slots: most characters (0 =
    ``TEXT_MAX``, never more)."""

    def __post_init__(self) -> None:
        if self.kind == "interval":
            object.__setattr__(self, "minimum", self.minimum or INTERVAL_MIN)
            object.__setattr__(self, "maximum", self.maximum or INTERVAL_MAX)
        elif self.kind == "text":
            object.__setattr__(self, "maximum", min(self.maximum or TEXT_MAX, TEXT_MAX))

    def check(self, raw: object) -> str:
        """The canonical text of ``raw`` for this slot, or ``BlueprintError``."""
        text = str(raw).strip() if raw is not None else ""
        if not text:
            if self.optional:
                return ""
            raise BlueprintError(f"{self.label} ({self.name}) needs a value")
        if self.kind == "time":
            return _check_time(self, text)
        if self.kind == "weekdays":
            return _check_weekdays(self, text)
        if self.kind == "choice":
            for option in self.options:
                if option.lower() == text.lower():
                    return option
            raise BlueprintError(
                f"{self.name} must be one of {', '.join(self.options)}; got {text!r}"
            )
        if self.kind == "interval":
            try:
                minutes = int(text.removesuffix("m"))
            except ValueError:
                raise BlueprintError(f"{self.name} is a number of minutes; got {text!r}") from None
            if not self.minimum <= minutes <= self.maximum:
                raise BlueprintError(
                    f"{self.name} must be between {self.minimum} and {self.maximum} minutes"
                )
            return str(minutes)
        if len(text) > self.maximum or any(ord(ch) < 32 for ch in text):
            raise BlueprintError(
                f"{self.name} must be one line of at most {self.maximum} characters"
            )
        return text


def _check_time(slot: Slot, text: str) -> str:
    found = _TIME.match(text)
    if found is None:
        raise BlueprintError(f"{slot.name} is a time like 08:30; got {text!r}")
    hour, minute = int(found.group(1)), int(found.group(2))
    if hour > 23 or minute > 59:
        raise BlueprintError(f"{slot.name} must be a time between 00:00 and 23:59; got {text!r}")
    return f"{hour:02d}:{minute:02d}"


def _check_weekdays(slot: Slot, text: str) -> str:
    lowered = text.lower()
    if lowered in WEEKDAY_PRESETS:
        return lowered
    days: set[int] = set()
    for part in re.split(r"[,\s]+", lowered):
        if not part:
            continue
        if part.isdigit():
            number = int(part)
            if number > 6:
                raise BlueprintError(
                    f"{slot.name}: day {number} is out of range (0 is Sunday, 6 is Saturday)"
                )
            days.add(number)
        elif part in _DAY_WORDS:
            days.add(_DAY_WORDS[part])
        else:
            raise BlueprintError(
                f"{slot.name} takes daily, weekdays, weekends or day names; got {part!r}"
            )
    if not days:
        raise BlueprintError(f"{slot.name} needs at least one day")
    return ",".join(_DAY_NAMES[day] for day in sorted(days))


def _dow_field(value: str) -> str:
    if value in WEEKDAY_PRESETS:
        return WEEKDAY_PRESETS[value]
    return ",".join(str(_DAY_NAMES.index(day)) for day in value.split(","))


@dataclass(frozen=True)
class Blueprint:
    """A job template (see the module notes). ``skill`` names the declaring skill, if any."""

    key: str
    title: str
    description: str
    schedule: str
    prompt: str
    slots: tuple[Slot, ...] = ()
    deliver: str = targets.ORIGIN
    skill: str | None = None
    integrations: tuple[str, ...] = ()

    @property
    def source(self) -> Literal["catalogue", "skill"]:
        return "skill" if self.skill else "catalogue"

    def slot(self, name: str) -> Slot | None:
        return next((s for s in self.slots if s.name == name), None)

    def fills_from_defaults(self) -> bool:
        return all(s.optional or s.default not in (None, "") for s in self.slots)


def _validate(bp: Blueprint) -> Blueprint:
    if not _KEY.match(bp.key.rpartition(":")[2]):
        raise BlueprintError(f"blueprint key {bp.key!r} is not a short lowercase name")
    if not bp.title.strip() or not bp.prompt.strip() or not bp.schedule.strip():
        raise BlueprintError(f"blueprint {bp.key!r} needs a title, a schedule and a prompt")
    names = [s.name for s in bp.slots]
    if len(set(names)) != len(names):
        raise BlueprintError(f"blueprint {bp.key!r} names a slot twice")
    kinds = {s.kind for s in bp.slots}
    for name in _PLACEHOLDER.findall(bp.schedule):
        needed = SCHEDULE_FIELDS.get(name)
        if needed is None:
            raise BlueprintError(
                f"blueprint {bp.key!r}: schedule placeholder {{{name}}} is not one of "
                + ", ".join("{" + k + "}" for k in SCHEDULE_FIELDS)
            )
        if needed not in kinds:
            raise BlueprintError(f"blueprint {bp.key!r}: no {needed} slot fills {{{name}}}")
    if "{" in _PLACEHOLDER.sub("", bp.schedule) or "}" in _PLACEHOLDER.sub("", bp.schedule):
        raise BlueprintError(f"blueprint {bp.key!r}: stray brace in the schedule")
    for slot in bp.slots:
        if slot.kind == "choice" and not slot.options:
            raise BlueprintError(f"blueprint {bp.key!r}: choice slot {slot.name} has no options")
        if slot.default not in (None, ""):
            slot.check(slot.default)
    problem = _deliver_problem(bp.deliver)
    if problem:
        raise BlueprintError(f"blueprint {bp.key!r}: {problem}")
    if len(bp.integrations) > MAX_INTEGRATIONS:
        raise BlueprintError(
            f"blueprint {bp.key!r} names more than {MAX_INTEGRATIONS} integrations"
        )
    for name in bp.integrations:
        if not _INTEGRATION.match(name):
            raise BlueprintError(
                f"blueprint {bp.key!r}: integration {name!r} is not a short lowercase name"
            )
    return bp


def _deliver_problem(value: str) -> str | None:
    parsed = targets.parse_targets(value)
    return None if parsed.ok else "; ".join(p.message for p in parsed.problems)


# -- filling ------------------------------------------------------------------------------------


def _values(bp: Blueprint, given: Mapping[str, object]) -> dict[str, str]:
    unknown = sorted(set(given) - {s.name for s in bp.slots})
    if unknown:
        known = ", ".join(s.name for s in bp.slots) or "none"
        raise BlueprintError(f"{bp.key} has no slot {', '.join(unknown)} (its slots: {known})")
    out: dict[str, str] = {}
    for slot in bp.slots:
        raw = given.get(slot.name)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            raw = slot.default
        out[slot.name] = slot.check(raw)
    return out


def _schedule_text(bp: Blueprint, values: Mapping[str, str]) -> str:
    by_kind: dict[str, str] = {}
    for slot in bp.slots:
        if slot.kind in ("time", "weekdays", "interval") and slot.kind not in by_kind:
            by_kind[slot.kind] = values[slot.name]
    fields: dict[str, str] = {}
    if "time" in by_kind:
        hour, minute = by_kind["time"].split(":")
        fields["hour"], fields["minute"] = str(int(hour)), str(int(minute))
    if "weekdays" in by_kind:
        fields["dow"] = _dow_field(by_kind["weekdays"])
    if "interval" in by_kind:
        fields["interval"] = by_kind["interval"]
    return _PLACEHOLDER.sub(lambda m: fields[m.group(1)], bp.schedule)


def _prompt_text(bp: Blueprint, values: Mapping[str, str]) -> str:
    def swap(found: re.Match[str]) -> str:
        name = found.group(1)
        return values[name] if name in values else found.group(0)

    return _PLACEHOLDER.sub(swap, bp.prompt).strip()


def fill(bp: Blueprint, given: Mapping[str, object] | None = None) -> dict[str, Any]:
    """The job spec for ``bp`` with ``given`` slot values (defaults for the rest)."""
    values = _values(bp, given or {})
    schedule = _schedule_text(bp, values)
    try:
        parsed = parse_schedule(schedule, UTC, now=datetime(2000, 1, 1, tzinfo=UTC))
    except ScheduleError as exc:
        raise BlueprintError(f"{bp.key}: the schedule {schedule!r} does not parse: {exc}") from None
    if not is_recurring(parsed):
        raise BlueprintError(f"{bp.key}: a blueprint schedule must recur; {schedule!r} does not")
    return {
        "name": bp.title,
        "schedule": schedule,
        "prompt": _prompt_text(bp, values),
        "deliver": bp.deliver,
    }


# -- rendering ----------------------------------------------------------------------------------


def form(bp: Blueprint) -> dict[str, Any]:
    """Fields for a dashboard or desktop form."""
    fields: list[dict[str, Any]] = []
    for slot in bp.slots:
        row: dict[str, Any] = {
            "name": slot.name,
            "kind": slot.kind,
            "label": slot.label,
            "default": slot.default,
            "optional": slot.optional,
            "help": slot.help,
        }
        if slot.kind == "choice":
            row["options"] = list(slot.options)
        if slot.kind == "weekdays":
            row["options"] = [*WEEKDAY_PRESETS, *_DAY_NAMES]
        if slot.kind == "interval":
            row["min"], row["max"] = slot.minimum, slot.maximum
        if slot.kind == "text":
            row["max"] = slot.maximum
        fields.append(row)
    return {
        "key": bp.key,
        "title": bp.title,
        "description": bp.description,
        "schedule": bp.schedule,
        "source": bp.source,
        "fields": fields,
    }


def cron_line(bp: Blueprint, given: Mapping[str, object] | None = None) -> str:
    """A ``/cron add`` line for ``bp`` filled from ``given`` and the defaults."""
    spec = fill(bp, given)
    words = ["--name", spec["name"]]
    words += ["--deliver", spec["deliver"]]
    return "/cron add " + shlex.join([*words, spec["schedule"], spec["prompt"]])


def seed(bp: Blueprint) -> str:
    """The prompt that has the agent gather the slot values and create the job."""
    lines = [
        f"Please set up the scheduled job '{bp.title}' for me from the blueprint {bp.key}. "
        f"{bp.description}".rstrip(),
        "",
        "Ask me for these one at a time; say what the default is so I can just accept it:",
    ]
    for slot in bp.slots:
        parts = [f"- {slot.label} ({slot.name}, {_kind_hint(slot)})"]
        if slot.default not in (None, ""):
            parts.append(f", default {slot.default}")
        if slot.optional:
            parts.append(", may be left empty")
        if slot.help:
            parts.append(f": {slot.help}")
        lines.append("".join(parts))
    lines += [
        "",
        f"Then create one job with the cronjob_manage tool (action create). Its schedule is "
        f"`{bp.schedule}`: {{minute}} and {{hour}} come from the time, {{dow}} from the days "
        f"(daily = *, weekdays = 1-5, weekends = 0,6, or day numbers with 0 for Sunday), "
        f"{{interval}} from the minutes. Its prompt is this text with my answers in place of "
        f'the {{slot}} names: "{bp.prompt}". Deliver to {bp.deliver}. Read the schedule and '
        f"the prompt back to me and wait for my yes before you create it.",
    ]
    return "\n".join(lines)


def _kind_hint(slot: Slot) -> str:
    if slot.kind == "time":
        return "a time like 08:30"
    if slot.kind == "weekdays":
        return "daily, weekdays, weekends or day names"
    if slot.kind == "choice":
        return "one of " + ", ".join(slot.options)
    if slot.kind == "interval":
        return f"minutes, {slot.minimum} to {slot.maximum}"
    return f"text, up to {slot.maximum} characters"


def describe_schedule(bp: Blueprint) -> str:
    """A short phrase for the schedule shape (``at a time you pick on the days you pick``)."""
    names = set(_PLACEHOLDER.findall(bp.schedule))
    if "interval" in names:
        return "every few minutes, as often as you pick"
    if names >= {"hour", "dow"}:
        return "at a time and on days you pick"
    if "hour" in names:
        return "at a time you pick"
    return f"on the schedule {bp.schedule}"


# -- the catalogue ------------------------------------------------------------------------------


def _time(default: str, label: str = "Time of day") -> Slot:
    return Slot("time", "time", label, default=default)


def _days(default: str) -> Slot:
    return Slot("days", "weekdays", "Days", default=default)


CATALOGUE: tuple[Blueprint, ...] = (
    Blueprint(
        key="morning-brief",
        title="Morning briefing",
        description="A short start-of-day summary of what you ask it to cover.",
        schedule="{minute} {hour} * * {dow}",
        prompt=(
            "Write my morning briefing. Cover: {topics}. Keep it under 200 words, lead with "
            "anything that needs action today, and say plainly when there is nothing new."
        ),
        slots=(
            _time("08:00"),
            _days("weekdays"),
            Slot(
                "topics",
                "text",
                "What to cover",
                default="the day's calendar, open reminders and the weather",
            ),
        ),
        integrations=("platform",),
    ),
    Blueprint(
        key="weekly-review",
        title="Weekly review",
        description="Looks back over the week's sessions and lists loose ends.",
        schedule="{minute} {hour} * * {dow}",
        prompt=(
            "Review what we worked on this week. List what was finished, what is still open and "
            "the three things most worth doing next week. Style: {style}."
        ),
        slots=(
            _time("16:30"),
            _days("fri"),
            Slot("style", "choice", "Level of detail", default="brief", options=("brief", "full")),
        ),
    ),
    Blueprint(
        key="check-in",
        title="Regular check-in",
        description="Checks something on a fixed interval and reports only changes.",
        schedule="every {interval}m",
        prompt=(
            "Check {target}. Report only what changed since the last check; if nothing "
            "changed, answer with the single word: unchanged."
        ),
        slots=(
            Slot("interval", "interval", "Minutes between checks", default="60", minimum=15),
            Slot("target", "text", "What to check", help="a page, a folder, a service"),
        ),
    ),
    Blueprint(
        key="evening-wrap",
        title="Evening wrap-up",
        description="Ends the day with what got done and what is waiting for tomorrow.",
        schedule="{minute} {hour} * * {dow}",
        prompt=(
            "Wrap up my day: what got done, what is waiting for tomorrow, and anything I "
            "promised someone. {extra}"
        ),
        slots=(
            _time("18:00"),
            _days("weekdays"),
            Slot("extra", "text", "Anything else to include", optional=True),
        ),
    ),
)
for _bp in CATALOGUE:
    _validate(_bp)


# -- skill blueprints ---------------------------------------------------------------------------


def _slot_from(raw: Mapping[str, Any]) -> Slot:
    name = str(raw.get("name") or "").strip()
    if not _SLOT_NAME.match(name):
        raise BlueprintError(f"slot name {name!r} is not a short lowercase name")
    kind = str(raw.get("kind") or raw.get("type") or "text").strip().lower()
    if kind not in SLOT_KINDS:
        raise BlueprintError(f"slot {name}: kind {kind!r} is not one of {sorted(SLOT_KINDS)}")
    options_raw: object = raw.get("options") or ()
    if not isinstance(options_raw, list | tuple):
        raise BlueprintError(f"slot {name}: options must be a list")
    default = raw.get("default")
    numbers: dict[str, int] = {}
    for key in ("min", "max"):
        value = raw.get(key, 0)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise BlueprintError(f"slot {name}: {key} must be a whole number of at least 0")
        numbers[key] = value
    return Slot(
        name=name,
        kind=cast(SlotKind, kind),
        label=str(raw.get("label") or name.replace("_", " ").capitalize()),
        default=None if default is None else str(default),
        options=tuple(str(o) for o in cast(Sequence[object], options_raw)),
        optional=raw.get("optional") is True,
        help=str(raw.get("help") or ""),
        minimum=numbers["min"],
        maximum=numbers["max"],
    )


def blueprint_from_mapping(raw: Mapping[str, Any], *, skill: str | None = None) -> Blueprint:
    """A checked blueprint from a frontmatter mapping; ``BlueprintError`` when it does not load."""
    key = str(raw.get("key") or "").strip().lower()
    if not _KEY.match(key):
        raise BlueprintError(f"blueprint key {key!r} is not a short lowercase name")
    slots_raw: object = raw.get("slots") or []
    if not isinstance(slots_raw, list):
        raise BlueprintError(f"blueprint {key}: slots must be a list")
    wanted: object = raw.get("integrations") or []
    if not isinstance(wanted, list):
        raise BlueprintError(f"blueprint {key}: integrations must be a list")
    slots: list[Slot] = []
    for item in cast(list[object], slots_raw):
        if not isinstance(item, Mapping):
            raise BlueprintError(f"blueprint {key}: each slot is a mapping")
        slots.append(_slot_from(cast(Mapping[str, Any], item)))
    bp = Blueprint(
        key=f"{skill}:{key}" if skill else key,
        title=str(raw.get("title") or "").strip(),
        description=str(raw.get("description") or "").strip(),
        schedule=" ".join(str(raw.get("schedule") or "").split()),
        prompt=str(raw.get("prompt") or "").strip(),
        slots=tuple(slots),
        deliver=str(raw.get("deliver") or targets.ORIGIN).strip(),
        skill=skill,
        integrations=tuple(
            dict.fromkeys(str(name).strip().lower() for name in cast(list[object], wanted))
        ),
    )
    return _validate(bp)


def _frontmatter(text: str) -> dict[str, Any]:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    try:
        data = yamlio.load_safe(text[3:end])
    except yamlio.YamlError:
        return {}
    return cast(dict[str, Any], data) if isinstance(data, dict) else {}


def _declared(meta: Mapping[str, Any]) -> list[object]:
    found: list[object] = []
    single = meta.get("blueprint")
    if isinstance(single, Mapping):
        found.append(single)
    metadata = meta.get("metadata")
    ours = cast(Mapping[str, Any], metadata).get("indus") if isinstance(metadata, Mapping) else None
    listed = cast(Mapping[str, Any], ours).get("blueprints") if isinstance(ours, Mapping) else None
    if isinstance(listed, list):
        found.extend(cast(list[object], listed))
    return found


def _skill_files(root: Path) -> Iterable[Path]:
    if not root.is_dir():
        return []
    files: list[Path] = []
    for path in sorted(root.rglob(SKILL_FILE)):
        rel = path.relative_to(root).parts
        if any(part.startswith(".") for part in rel):
            continue
        files.append(path)
        if len(files) >= MAX_SKILL_FILES:
            break
    return files


def skill_blueprints(scope: Scope) -> list[Blueprint]:
    """Blueprints declared by the profile's skills (bad ones skipped with a warning)."""
    out: list[Blueprint] = []
    for path in _skill_files(scope.home / "skills"):
        try:
            meta = _frontmatter(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError):
            continue
        declared = _declared(meta)
        if not declared:
            continue
        skill = str(meta.get("name") or path.parent.name).strip().lower()
        for raw in declared:
            if not isinstance(raw, Mapping):
                log.warning("skill %s: a blueprint entry is not a mapping; skipped", skill)
                continue
            try:
                out.append(blueprint_from_mapping(cast(Mapping[str, Any], raw), skill=skill))
            except BlueprintError as exc:
                log.warning("skill %s: blueprint skipped: %s", skill, exc)
    return out


def list_blueprints(scope: Scope) -> list[Blueprint]:
    """The starter catalogue, then skill blueprints by key (a key seen twice keeps the first)."""
    seen: set[str] = set()
    out: list[Blueprint] = []
    for bp in [*CATALOGUE, *sorted(skill_blueprints(scope), key=lambda b: b.key)]:
        if bp.key not in seen:
            seen.add(bp.key)
            out.append(bp)
    return out


def match_blueprint(
    blueprints: Sequence[Blueprint], query: str
) -> tuple[Blueprint | None, list[Blueprint]]:
    """``(one, [])`` for a unique match, ``(None, several)`` when ambiguous, ``(None, [])`` for
    none."""
    q = query.strip().lower()
    if not q:
        return None, []
    for bp in blueprints:
        if bp.key == q:
            return bp, []

    def starts(bp: Blueprint) -> bool:
        keys = (bp.key, bp.key.rpartition(":")[2])
        return any(k.startswith(q) for k in keys) or any(
            w.lower().startswith(q) for w in bp.title.split()
        )

    def inside(bp: Blueprint) -> bool:
        return q in bp.key or q in bp.title.lower() or q in bp.description.lower()

    for test in (starts, inside):
        found = [bp for bp in blueprints if test(bp)]
        if len(found) == 1:
            return found[0], []
        if found:
            return None, found
    by_key = {bp.key: bp for bp in blueprints}
    close = [by_key[k] for k in difflib.get_close_matches(q, list(by_key), n=3, cutoff=0.6)]
    if len(close) == 1:
        return close[0], []
    return None, close


def near_names(blueprints: Sequence[Blueprint], query: str) -> list[str]:
    """Keys that look like ``query`` (for a "did you mean" line)."""
    keys = [bp.key for bp in blueprints]
    return difflib.get_close_matches(query.strip().lower(), keys, n=3, cutoff=0.4)


# -- the /blueprint command ---------------------------------------------------------------------


@dataclass(frozen=True)
class BlueprintReply:
    """What ``/blueprint`` shows; with ``seed`` set the surface also sends it to the agent."""

    text: str
    seed: str | None = None
    job_id: str | None = None


def _manage(surface: str) -> str:
    if surface in ("cli", "tui"):
        return "/cron lists, pauses and removes it."
    return "Ask me any time to list, pause or remove it."


def split_values(words: Iterable[str]) -> tuple[dict[str, str], list[str]]:
    """``slot=value`` words as a mapping, and the words without ``=``."""
    values: dict[str, str] = {}
    rest: list[str] = []
    for word in words:
        key, sep, value = word.partition("=")
        if sep and key.strip():
            values[key.strip().lower()] = value
        else:
            rest.append(word)
    return values, rest


def catalogue_text(blueprints: Sequence[Blueprint]) -> str:
    lines = ["Job blueprints (`/blueprint <name>` and I will ask for the details):", ""]
    for bp in blueprints:
        where = f" (from the {bp.skill} skill)" if bp.skill else ""
        lines.append(f"  {bp.key}: {bp.title}{where}")
        if bp.description:
            lines.append(f"    {bp.description}")
    lines += [
        "",
        "Give values inline to skip the questions, e.g. /blueprint morning-brief time=07:30",
    ]
    return "\n".join(lines)


def handle_blueprint(
    scope: Scope,
    args: str,
    *,
    backend: Callable[[], ports.CronBackend],
    surface: str = "cli",
    origin: Mapping[str, Any] | None = None,
) -> BlueprintReply:
    """Run one ``/blueprint`` line (see the module notes)."""
    try:
        words = shlex.split(args)
    except ValueError:
        words = args.split()
    blueprints = list_blueprints(scope)
    if not words:
        return BlueprintReply(catalogue_text(blueprints))
    values, rest = split_values(words[1:])
    bp, several = match_blueprint(blueprints, words[0])
    if bp is None:
        if several:
            names = "\n".join(f"  {b.key}: {b.title}" for b in several)
            return BlueprintReply(f"'{words[0]}' matches several blueprints:\n{names}")
        near = near_names(blueprints, words[0])
        hint = f" Did you mean {', '.join(near)}?" if near else ""
        return BlueprintReply(f"No blueprint matches '{words[0]}'.{hint} /blueprint lists them.")
    if rest:
        return BlueprintReply(f"Give values as slot=value; not understood: {' '.join(rest)}")
    if not values:
        return BlueprintReply(
            f"Setting up '{bp.title}' ({describe_schedule(bp)}); a few questions first.",
            seed=seed(bp),
        )
    try:
        spec = fill(bp, values)
    except BlueprintError as exc:
        return BlueprintReply(
            f"Can't set up '{bp.title}': {exc}\n"
            f"Or run /blueprint {bp.key} and answer the questions."
        )
    if origin is not None:
        spec = {**spec, "origin": dict(origin)}
    try:
        job = backend().create(spec)
    except (ValueError, CronStoreBusy, ports.StoreUnavailable) as exc:
        return BlueprintReply(f"The job was not created: {exc}")
    job_id = str(ports.field(job, "id"))
    shown = ports.field(job, "schedule_display") or spec["schedule"]
    return BlueprintReply(
        f"Scheduled '{bp.title}' ({shown}) as job {job_id}, delivering to {spec['deliver']}. "
        f"{_manage(surface)}",
        job_id=job_id,
    )
