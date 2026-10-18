"""The classic REPL's bottom status bar (P06-06).

The bar is rebuilt from a ``StatusSnapshot`` on every refresh. What it shows depends on the
terminal width:

- full (``FULL_WIDTH`` columns and more): model │ context used/max │ fill bar and percentage │
  cost │ compressions │ background jobs │ turn time │ title │ auto-approve warning;
- compact (``COMPACT_WIDTH`` up to the full width): model · percentage · cost · compressions ·
  background jobs · turn time · title · warning;
- minimal (narrower): model · turn time · warning.

Counts that are zero are left out. The context percentage is coloured by how full the window is
(``context_level``). The finished line is cut to the width so it never wraps.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth

__all__ = [
    "COMPACT_WIDTH",
    "FULL_WIDTH",
    "MODEL_MAX",
    "STATUS_STYLE",
    "ContextLevel",
    "StatusSnapshot",
    "compact_tokens",
    "context_level",
    "context_percent",
    "fill_bar",
    "format_duration",
    "short_model",
    "status_fragments",
    "status_text",
    "tier_for",
]

FULL_WIDTH = 76
"""From this many columns the bar shows every segment."""
COMPACT_WIDTH = 52
"""From this many columns (below ``FULL_WIDTH``) the bar is compact; narrower is minimal."""
MODEL_MAX = 26
"""Longest model name shown before it is cut."""
MODEL_MIN = 8
"""The model name is never shortened below this to make room for other segments."""
BAR_CELLS = 10
TITLE_MAX = 24

type ContextLevel = Literal["ok", "warm", "hot", "full", "unknown"]
type Tier = Literal["full", "compact", "minimal"]

STATUS_STYLE = Style.from_dict(
    {
        "bottom-toolbar": "noreverse bg:#1d2330 #c8cdd8",
        "status.model": "bold #e8ecf3",
        "status.dim": "#8a93a6",
        "status.sep": "#596276",
        "status.ctx-ok": "#5fbf77",
        "status.ctx-warm": "#d7c25a",
        "status.ctx-hot": "#e08a3c",
        "status.ctx-full": "bold #e5534b",
        "status.ctx-unknown": "#8a93a6",
        "status.warn": "bold #e5534b",
        "status.title": "italic #b9c2d6",
        "status.activity": "#7fb4e8",
    }
)
"""Styles the bar's fragments refer to (merged into the REPL's style)."""


@dataclass(frozen=True)
class StatusSnapshot:
    """What the status bar shows at one moment."""

    model: str = ""
    context_used: int | None = None
    context_max: int | None = None
    context_estimated: bool = False
    """True when part of ``context_used`` is a local estimate (shown with ``~``)."""
    cost: float | None = None
    """Spend so far in USD; None until pricing exists (P08)."""
    compressions: int = 0
    background_jobs: int = 0
    turn_seconds: float | None = None
    """How long the running turn has taken, or the last turn took; None before the first."""
    turn_live: bool = False
    title: str = ""
    auto_approve: bool = False


def tier_for(width: int) -> Tier:
    if width >= FULL_WIDTH:
        return "full"
    if width >= COMPACT_WIDTH:
        return "compact"
    return "minimal"


def short_model(name: str) -> str:
    """The model's own name (no ``vendor/`` path), cut with an ellipsis past ``MODEL_MAX``."""
    shown = name.rsplit("/", 1)[-1] if name else ""
    if not shown:
        return "no model"
    if len(shown) > MODEL_MAX:
        return shown[: MODEL_MAX - 1] + "…"
    return shown


def context_percent(snapshot: StatusSnapshot) -> int | None:
    used, top = snapshot.context_used, snapshot.context_max
    if used is None or not top or top <= 0:
        return None
    return max(0, min(100, round(used * 100 / top)))


def context_level(percent: int | None) -> ContextLevel:
    """Under 50 % ok, 50–80 % warm, above 80 % hot, from 95 % full."""
    if percent is None:
        return "unknown"
    if percent >= 95:
        return "full"
    if percent > 80:
        return "hot"
    if percent >= 50:
        return "warm"
    return "ok"


def compact_tokens(count: int) -> str:
    """``950``, ``12K``, ``1.5K``, ``200K``, ``1M``, ``1.2M``."""
    if count < 1000:
        return str(count)
    for size, unit in ((1_000_000, "M"), (1000, "K")):
        if count >= size:
            value = count / size
            text = f"{value:.1f}" if value < 10 else f"{value:.0f}"
            return text.removesuffix(".0") + unit
    return str(count)  # pragma: no cover - the loop always returns


def fill_bar(percent: int | None, cells: int = BAR_CELLS) -> str:
    share = max(0, min(100, percent or 0))
    filled = round(share * cells / 100)
    return "▰" * filled + "▱" * (cells - filled)


def format_duration(seconds: float) -> str:
    """``0s``, ``42s``, ``3m 05s``, ``1h 02m``."""
    whole = max(0, int(seconds))
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


type Segment = tuple[str, StyleAndTextTuples]

DROP_ORDER = ("title", "context", "jobs", "compressions", "cost", "bar", "duration", "percent")
"""Segments given up first when the bar does not fit; the model and the warning always stay."""


def _segments(snapshot: StatusSnapshot, tier: Tier) -> list[Segment]:
    segs: list[Segment] = [("model", [("class:status.model", short_model(snapshot.model))])]
    percent = context_percent(snapshot)
    level = context_level(percent)
    mark = "~" if snapshot.context_estimated else ""
    pct_style = f"class:status.ctx-{level}"
    pct_text = f"{mark}{percent}%" if percent is not None else "--"
    if tier == "full":
        if snapshot.context_max:
            used = "?" if snapshot.context_used is None else compact_tokens(snapshot.context_used)
            detail = f"{mark}{used}/{compact_tokens(snapshot.context_max)}"
        else:
            detail = "context ?"
        segs.append(("context", [("class:status.dim", detail)]))
        segs.append(("bar", [(pct_style, fill_bar(percent)), ("", " "), (pct_style, pct_text)]))
    if tier != "minimal":
        if tier == "compact":
            segs.append(("percent", [(pct_style, pct_text)]))
        cost = "n/a" if snapshot.cost is None else f"${snapshot.cost:.2f}"
        segs.append(("cost", [("class:status.dim", f"cost {cost}")]))
        if snapshot.compressions:
            count = snapshot.compressions
            style = "ctx-hot" if count >= 10 else "ctx-warm" if count >= 5 else "dim"
            segs.append(("compressions", [(f"class:status.{style}", f"⇣{count}")]))
        if snapshot.background_jobs:
            segs.append(("jobs", [("class:status.dim", f"⚙{snapshot.background_jobs}")]))
    if snapshot.turn_seconds is not None:
        glyph = "⏱" if snapshot.turn_live else "✓"
        text = f"{glyph} {format_duration(snapshot.turn_seconds)}"
        segs.append(("duration", [("class:status.dim", text)]))
    if tier != "minimal" and snapshot.title:
        title = snapshot.title
        if len(title) > TITLE_MAX:
            title = title[: TITLE_MAX - 1] + "…"
        segs.append(("title", [("class:status.title", title)]))
    if snapshot.auto_approve:
        segs.append(("warning", [("class:status.warn", "⚠ auto-approve")]))
    return segs


def _join(segs: list[Segment], sep: str) -> StyleAndTextTuples:
    out: StyleAndTextTuples = [("", " ")]
    for index, (_, fragments) in enumerate(segs):
        if index:
            out.append(("class:status.sep", sep))
        out.extend(fragments)
    return out


def _width_of(fragments: StyleAndTextTuples) -> int:
    return sum(get_cwidth(text) for _, text, *_ in fragments)


def _cut(fragments: StyleAndTextTuples, width: int) -> StyleAndTextTuples:
    """Keep at most ``width`` cells; the last kept cell becomes ``…`` when something is cut."""
    if _width_of(fragments) <= width:
        return fragments
    room = max(0, width - 1)
    out: StyleAndTextTuples = []
    for style, text, *_ in fragments:
        if room <= 0:
            break
        kept = ""
        for char in text:
            cells = get_cwidth(char)
            if cells > room:
                room = 0
                break
            kept += char
            room -= cells
        if kept:
            out.append((style, kept))
    out.append(("class:status.dim", "…"))
    return out


def status_fragments(snapshot: StatusSnapshot, width: int) -> StyleAndTextTuples:
    """The bar for ``width`` columns as prompt_toolkit style fragments. When it does not fit,
    segments are given up in ``DROP_ORDER``; what still does not fit is cut."""
    width = max(1, width)
    tier = tier_for(width)
    sep = " │ " if tier == "full" else " · "
    segs = _segments(snapshot, tier)
    out = _join(segs, sep)
    for name in DROP_ORDER:
        if _width_of(out) <= width:
            break
        segs = [seg for seg in segs if seg[0] != name]
        out = _join(segs, sep)
    overflow = _width_of(out) - width
    if overflow > 0 and len(segs) > 1:  # shorten the model name before the warning is cut
        model = short_model(snapshot.model)
        keep = max(MODEL_MIN, len(model) - overflow)
        if keep < len(model):
            segs[0] = ("model", [("class:status.model", model[: keep - 1] + "…")])
            out = _join(segs, sep)
    return _cut(out, width)


def status_text(snapshot: StatusSnapshot, width: int) -> str:
    """The bar for ``width`` columns as plain text."""
    return "".join(text for _, text, *_ in status_fragments(snapshot, width))
