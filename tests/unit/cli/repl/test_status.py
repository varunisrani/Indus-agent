"""Banner, recap and status bar (P06-06)."""

from __future__ import annotations

from pathlib import Path

import pytest
from prompt_toolkit.utils import get_cwidth

from indus.cli.repl.banner import (
    BannerInfo,
    RecapSettings,
    banner,
    collect_recap,
    recap,
    recap_panel,
)
from indus.cli.repl.status import (
    StatusSnapshot,
    compact_tokens,
    context_level,
    context_percent,
    fill_bar,
    format_duration,
    short_model,
    status_fragments,
    status_text,
    tier_for,
)
from indus.core.transcript import TranscriptRow
from tests.unit.cli.repl.conftest import recording_console

TYPICAL = StatusSnapshot(
    model="vendor/big-model-2",
    context_used=50_000,
    context_max=200_000,
    context_estimated=True,
    compressions=1,
    turn_seconds=42,
    turn_live=True,
    title="Demo run",
)
CROWDED = StatusSnapshot(
    model="router/vendor/some-really-long-model-name-v2",
    context_used=123_400,
    context_max=200_000,
    context_estimated=True,
    compressions=2,
    background_jobs=1,
    turn_seconds=75,
    turn_live=True,
    title="Fix the build",
    auto_approve=True,
)


# -- width tiers ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("snapshot", "width", "expected"),
    [
        (TYPICAL, 40, " big-model-2 · ⏱ 42s"),
        (TYPICAL, 60, " big-model-2 · ~25% · cost n/a · ⇣1 · ⏱ 42s · Demo run"),
        (
            TYPICAL,
            100,
            " big-model-2 │ ~50K/200K │ ▰▰▱▱▱▱▱▱▱▱ ~25% │ cost n/a │ ⇣1 │ ⏱ 42s │ Demo run",
        ),
        (CROWDED, 40, " some-really-long-mode… · ⚠ auto-approve"),
        (CROWDED, 60, " some-really-long-model-na… · ~62% · ⚠ auto-approve"),
        (
            CROWDED,
            100,
            " some-really-long-model-na… │ ▰▰▰▰▰▰▱▱▱▱ ~62% │ cost n/a │ ⇣2 │ ⚙1 │ ⏱ 1m 15s │ "
            "⚠ auto-approve",
        ),
        (StatusSnapshot(model="m"), 100, " m │ context ? │ ▱▱▱▱▱▱▱▱▱▱ -- │ cost n/a"),
        (StatusSnapshot(model="m", turn_seconds=3), 40, " m · ✓ 3s"),
    ],
)
def test_width_tier_goldens(snapshot: StatusSnapshot, width: int, expected: str) -> None:
    assert status_text(snapshot, width) == expected


@pytest.mark.parametrize("width", [20, 30, 40, 51, 52, 60, 75, 76, 100, 160])
def test_the_bar_never_wraps(width: int) -> None:
    for snapshot in (TYPICAL, CROWDED):
        assert get_cwidth(status_text(snapshot, width)) <= width


def test_tier_boundaries() -> None:
    assert [tier_for(w) for w in (51, 52, 75, 76)] == ["minimal", "compact", "compact", "full"]


@pytest.mark.parametrize(
    ("percent", "level"),
    [
        (None, "unknown"),
        (0, "ok"),
        (49, "ok"),
        (50, "warm"),
        (80, "warm"),
        (81, "hot"),
        (94, "hot"),
        (95, "full"),
        (100, "full"),
    ],
)
def test_context_colour_thresholds(percent: int | None, level: str) -> None:
    assert context_level(percent) == level


def test_context_style_reaches_the_fragments() -> None:
    hot = StatusSnapshot(model="m", context_used=90, context_max=100)
    styles = {style for style, _text, *_ in status_fragments(hot, 100)}
    assert "class:status.ctx-hot" in styles


def test_context_percent_edges() -> None:
    assert context_percent(StatusSnapshot(context_used=10, context_max=0)) is None
    assert context_percent(StatusSnapshot(context_used=None, context_max=100)) is None
    assert context_percent(StatusSnapshot(context_used=500, context_max=100)) == 100


@pytest.mark.parametrize(
    ("name", "shown"),
    [
        ("", "no model"),
        ("vendor/short", "short"),
        ("a" * 26, "a" * 26),
        ("a" * 27, "a" * 25 + "…"),
    ],
)
def test_model_truncated_past_26(name: str, shown: str) -> None:
    assert short_model(name) == shown


def test_small_formatters() -> None:
    assert [
        compact_tokens(n) for n in (950, 1000, 1500, 12_000, 200_000, 1_000_000, 1_250_000)
    ] == [
        "950",
        "1K",
        "1.5K",
        "12K",
        "200K",
        "1M",
        "1.2M",
    ]
    assert fill_bar(0) == "▱" * 10 and fill_bar(100) == "▰" * 10 and fill_bar(None) == "▱" * 10
    assert [format_duration(s) for s in (0, 42, 185, 3720)] == ["0s", "42s", "3m 05s", "1h 02m"]


# -- banner ---------------------------------------------------------------------------------------


def test_banner_names_model_backend_folder_and_counts(tmp_path: Path) -> None:
    console = recording_console(100)
    info = BannerInfo(
        version="0.0.1",
        model="big-model-2",
        provider="fakehost",
        backend="local",
        cwd=tmp_path / "proj",
        tool_count=12,
        skill_count=1,
    )
    console.print(banner(info, user_home=tmp_path))
    text = console.export_text()
    for part in (
        "Indus Agent 0.0.1",
        "big-model-2 (fakehost)",
        "run on the local backend",
        "~/proj",
        "12 tools · 1 skill",
        "/help lists commands",
        "ready",
    ):
        assert part in text


# -- recap ----------------------------------------------------------------------------------------


def rows_for(pairs: list[tuple[str, str]]) -> list[dict[str, str]]:
    return [{"role": role, "content": content} for role, content in pairs]


def test_recap_truncation_table() -> None:
    long_user = "u" * 400
    many_lines = "\n".join(f"line {n}" for n in range(6))
    long_answer = "a" * 500
    rows = rows_for(
        [
            ("user", long_user),
            ("assistant", many_lines),
            ("user", "short"),
            ("assistant", long_answer),
            ("user", "last question"),
            ("assistant", "final\nanswer\nwith\nmany\nlines"),
        ]
    )
    view = collect_recap(rows, RecapSettings())
    texts = [(e.role, e.text) for e in view.entries]
    assert texts[0] == ("user", "u" * 299 + "…")
    assert texts[1] == ("assistant", "line 0\nline 1\nline 2 …")
    assert texts[3] == ("assistant", "a" * 199 + "…")
    assert texts[5] == ("assistant_last", "final\nanswer\nwith\nmany\nlines")
    assert view.hidden == 0


def test_recap_keeps_the_last_exchanges_and_counts_the_rest() -> None:
    rows = rows_for([(r, f"{r} {n}") for n in range(15) for r in ("user", "assistant")])
    view = collect_recap(rows, RecapSettings(exchanges=10))
    assert view.hidden == 10
    assert view.entries[0].text == "user 5"
    assert view.entries[-1] == view.entries[-1].__class__("assistant_last", "assistant 14")


def test_recap_skips_tool_rows_hidden_rows_and_cleans_escapes() -> None:
    rows: list[object] = [
        TranscriptRow(role="system", content="prompt"),
        TranscriptRow(role="user", content="run \x1b[2Jit"),
        TranscriptRow(role="assistant", content=None, tool_calls=[{"id": "c"}]),
        TranscriptRow(role="tool", content="output", tool_call_id="c"),
        TranscriptRow(role="user", content="scaffold", scaffold=True),
        {"role": "user", "content": "hidden", "display_kind": "hidden"},
        {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
    ]
    view = collect_recap(rows)
    assert [(e.role, e.text) for e in view.entries] == [
        ("user", "run it"),
        ("assistant_last", "done"),
    ]


def test_recap_panel_renders_and_empty_history_renders_nothing() -> None:
    console = recording_console(80)
    console.print(recap(rows_for([("user", "hi"), ("assistant", "hello there")])))
    text = console.export_text()
    assert "earlier in this conversation" in text
    assert "you   hi" in text and "indus hello there" in text
    assert recap_panel(collect_recap([])) is None


def test_recap_settings_from_config_values() -> None:
    values = {"display.resume_exchanges": "4", "display.resume_max_user_chars": -1}
    settings = RecapSettings.from_config(lambda key, default: values.get(key, default))
    assert settings.exchanges == 4
    assert settings.max_user_chars == 300
    assert settings.max_assistant_lines == 3


@pytest.mark.parametrize(("count", "style"), [(1, "dim"), (5, "ctx-warm"), (10, "ctx-hot")])
def test_compression_count_colour(count: int, style: str) -> None:
    fragments = status_fragments(StatusSnapshot(model="m", compressions=count), 100)
    assert (f"class:status.{style}", f"⇣{count}") in [(f[0], f[1]) for f in fragments]
