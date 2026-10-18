"""Streaming render (P06-05): recorded event streams rendered to a recording console."""

from __future__ import annotations

from typing import Any

import pytest

from indus.cli.repl.cards import open_card
from indus.cli.repl.render import (
    RING_SIZE,
    StreamRenderer,
    argument_preview,
    format_elapsed,
    strip_markdown_line,
)
from indus.protocol.events import (
    EventBase,
    ExitReason,
    MessageDelta,
    Notice,
    NoticeLevel,
    ReasoningDelta,
    Status,
    StatusKind,
    ToolCompleted,
    ToolStarted,
    TurnDone,
    TurnFailure,
    TurnStarted,
)
from indus.safety.redaction import redact_output
from tests.unit.cli.repl.conftest import load_stream, recording_console

S = "20260101_090000_rec001"


def replay(renderer: StreamRenderer, items: list[Any]) -> None:
    for item in items:
        if item == "interrupt":
            renderer.seal()
        elif isinstance(item, EventBase):
            renderer.handle(item)
        else:  # a server request: the REPL shows its card
            card = open_card(item, 0.0, 80)
            assert card is not None
            for line in card.body(80):
                renderer.emit(line)


def render(name: str, **options: Any) -> str:
    console = recording_console()
    replay(StreamRenderer(console, **options), load_stream(name))
    return console.export_text()


class Seq:
    def __init__(self) -> None:
        self.n = 0

    def __call__(self) -> int:
        self.n += 1
        return self.n


@pytest.fixture
def seq() -> Seq:
    return Seq()


def started(seq: Seq, turn: str = "t1") -> TurnStarted:
    return TurnStarted(session_id=S, seq=seq(), turn_id=turn, generation=1)


def delta(seq: Seq, text: str, turn: str = "t1") -> MessageDelta:
    return MessageDelta(session_id=S, seq=seq(), turn_id=turn, text=text)


def done(
    seq: Seq, turn: str = "t1", reason: ExitReason = ExitReason.COMPLETED, **kw: Any
) -> TurnDone:
    return TurnDone(session_id=S, seq=seq(), turn_id=turn, reason=reason, **kw)


# -- goldens from the recorded streams ------------------------------------------------------------


def test_plain_answer_golden() -> None:
    assert render("plain_answer") == (
        "\n◇ indus\n  Hello! Here is a list:\n  - one\n  - **two**\n  Done.\n"
    )


def test_tool_trail_golden() -> None:
    assert render("tool_trail") == (
        "  ✓ 📖 read_file  notes.txt  0.0s\n"
        "  ✓ 💻 terminal  python hello.py  0.0s\n"
        "\n"
        "◇ indus\n"
        "  The script printed a greeting.\n"
        "  All good.\n"
    )


def test_interrupt_mid_delta_golden() -> None:
    assert render("interrupt_mid_delta") == (
        "\n◇ indus\n  Here is the first line\n  and a second line that \n  (interrupted)\n"
    )


def test_approval_request_golden_with_redacted_previews() -> None:
    text = render("approval_request", redact=lambda s: redact_output(s, force=True))
    assert text == (
        "\n"
        "⚠ Approval needed: sends data to a remote host\n"
        "    curl -H 'Authorization: Bearer rec0…mnop' https://example.invalid/upload\n"
        "  1  allow once\n"
        "  2  allow for the rest of this session\n"
        "  3  always allow this command (saved)\n"
        "  4  deny\n"
        # tool.started arrives masked at the source; the renderer's own mask (defence in depth)
        # masks the short remainder again.
        "  ✓ 💻 terminal  curl -H 'Authorization: Bearer [redacted]' https://example.…  "
        "0.0s\n"
        "\n"
        "◇ indus\n"
        "  Uploaded.\n"
    )
    assert "0123456789" not in text


def test_strip_mode_on_a_recorded_stream() -> None:
    assert "  - two\n" in render("plain_answer", final_markdown="strip")


# -- sealing (R05.4) ------------------------------------------------------------------------------


def test_deltas_after_an_interrupt_are_ignored(seq: Seq) -> None:
    console = recording_console()
    renderer = StreamRenderer(console)
    renderer.handle(started(seq))
    renderer.handle(delta(seq, "partial answ"))
    renderer.seal()
    renderer.handle(delta(seq, "er that came late\nmore\n"))
    renderer.handle(ReasoningDelta(session_id=S, seq=seq(), turn_id="t1", text="late\n"))
    renderer.handle(
        ToolCompleted(
            session_id=S,
            seq=seq(),
            turn_id="t1",
            call_id="c",
            name="terminal",
            ok=True,
            duration_ms=5,
            result="x",
        )
    )
    renderer.handle(done(seq, reason=ExitReason.INTERRUPTED_BY_USER))
    text = console.export_text()
    assert text == "\n◇ indus\n  partial answ\n  (interrupted)\n"


def test_seal_twice_or_when_idle_prints_nothing_more(seq: Seq) -> None:
    console = recording_console()
    renderer = StreamRenderer(console)
    renderer.seal()
    renderer.handle(started(seq))
    renderer.seal()
    renderer.seal()
    assert console.export_text().count("(interrupted)") == 1


def test_the_next_turn_renders_after_a_sealed_one(seq: Seq) -> None:
    console = recording_console()
    renderer = StreamRenderer(console)
    renderer.handle(started(seq, "t1"))
    renderer.seal()
    renderer.handle(done(seq, "t1", ExitReason.INTERRUPTED_BY_USER))
    renderer.handle(started(seq, "t2"))
    renderer.handle(delta(seq, "fresh\n", "t2"))
    renderer.handle(done(seq, "t2"))
    assert "  fresh\n" in console.export_text()


def test_interrupt_seen_only_at_turn_end_is_marked_once(seq: Seq) -> None:
    console = recording_console()
    renderer = StreamRenderer(console)
    renderer.handle(started(seq))
    renderer.handle(delta(seq, "half"))
    renderer.handle(done(seq, reason=ExitReason.INTERRUPTED_BY_SYSTEM))
    assert console.export_text() == "\n◇ indus\n  half\n  (interrupted)\n"


# -- reasoning, notices, endings ------------------------------------------------------------------


def reasoning_stream(seq: Seq) -> list[EventBase]:
    return [
        started(seq),
        ReasoningDelta(session_id=S, seq=seq(), turn_id="t1", text="weigh the "),
        ReasoningDelta(session_id=S, seq=seq(), turn_id="t1", text="options\nthen pick"),
        delta(seq, "Answer.\n"),
        done(seq),
    ]


def test_reasoning_hidden_by_default(seq: Seq) -> None:
    console = recording_console()
    renderer = StreamRenderer(console)
    for event in reasoning_stream(seq):
        renderer.handle(event)
    assert console.export_text() == "\n◇ indus\n  Answer.\n"


def test_reasoning_shown_dim_when_switched_on(seq: Seq) -> None:
    console = recording_console()
    renderer = StreamRenderer(console, show_reasoning=True)
    for event in reasoning_stream(seq):
        renderer.handle(event)
    assert console.export_text() == (
        "  ┆ reasoning\n  ┆ weigh the options\n  ┆ then pick\n\n◇ indus\n  Answer.\n"
    )


def test_model_text_is_never_read_as_markup(seq: Seq) -> None:
    console = recording_console()
    renderer = StreamRenderer(console)
    renderer.handle(started(seq))
    renderer.handle(delta(seq, "[bold red]not markup[/bold red] \x1b[31mno colour\x1b[0m\n"))
    renderer.handle(done(seq))
    assert "  [bold red]not markup[/bold red] no colour\n" in console.export_text()


def test_notices_statuses_and_failures(seq: Seq) -> None:
    console = recording_console()
    renderer = StreamRenderer(console)
    renderer.handle(started(seq))
    renderer.handle(Status(session_id=S, seq=seq(), kind=StatusKind.RETRYING, text="retry 1 of 3"))
    renderer.handle(
        Notice(session_id=S, seq=seq(), level=NoticeLevel.WARNING, text="slow", key="k")
    )
    renderer.handle(
        Notice(session_id=S, seq=seq(), level=NoticeLevel.WARNING, text="slow", key="k")
    )
    renderer.handle(Notice(session_id=S, seq=seq(), level=NoticeLevel.INFO, text="fyi"))
    renderer.handle(
        done(
            seq,
            reason=ExitReason.PROVIDER_FAILED,
            failure=TurnFailure(reason="rate limited", retryable=True),
        )
    )
    renderer.handle(started(seq, "t2"))
    renderer.handle(done(seq, "t2", ExitReason.MAX_ITERATIONS))
    assert console.export_text() == (
        "  ↻ retry 1 of 3\n"
        "  ! slow\n"
        "  · fyi\n"
        "  ✗ rate limited (trying again later may work)\n"
        "  ! stopped: the turn reached its step limit\n"
    )


def test_a_completed_turn_without_text_says_so(seq: Seq) -> None:
    console = recording_console()
    renderer = StreamRenderer(console)
    renderer.handle(started(seq))
    renderer.handle(done(seq))
    assert console.export_text() == "  (the reply had no text)\n"


def test_activity_names_the_running_tool_then_the_partial_line(seq: Seq) -> None:
    renderer = StreamRenderer(recording_console())
    renderer.handle(started(seq))
    assert renderer.activity() == "thinking…"
    renderer.handle(
        ToolStarted(
            session_id=S,
            seq=seq(),
            turn_id="t1",
            call_id="c1",
            name="search_files",
            arguments={"pattern": "TODO", "path": "src"},
        )
    )
    assert renderer.activity() == "🔎 search_files src"
    renderer.handle(
        ToolCompleted(
            session_id=S,
            seq=seq(),
            turn_id="t1",
            call_id="c1",
            name="search_files",
            ok=False,
            duration_ms=61_500,
            result="boom",
        )
    )
    renderer.handle(delta(seq, "Writing the summary now"))
    assert renderer.activity() == "Writing the summary now"
    renderer.handle(done(seq))
    assert renderer.activity() == ""


def test_failed_tool_line_and_long_durations(seq: Seq) -> None:
    console = recording_console()
    renderer = StreamRenderer(console)
    renderer.handle(started(seq))
    renderer.handle(
        ToolStarted(
            session_id=S, seq=seq(), turn_id="t1", call_id="c1", name="mystery", arguments={}
        )
    )
    renderer.handle(
        ToolCompleted(
            session_id=S,
            seq=seq(),
            turn_id="t1",
            call_id="c1",
            name="mystery",
            ok=False,
            duration_ms=61_500,
            result="boom",
        )
    )
    assert console.export_text() == "  ✗ 🔧 mystery  1m 01s\n"


def test_output_ring_keeps_the_last_lines(seq: Seq) -> None:
    renderer = StreamRenderer(recording_console())
    renderer.handle(started(seq))
    renderer.handle(delta(seq, "".join(f"line {n}\n" for n in range(RING_SIZE + 50))))
    assert len(renderer.ring) == RING_SIZE
    assert renderer.ring[-1] == f"  line {RING_SIZE + 49}"


# -- helpers --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "shown"),
    [
        ("# Title", "Title"),
        ("### Deeper", "Deeper"),
        ("```python", None),
        ("~~~", None),
        ("---", ""),
        (
            "Some **bold** and *italic* and __under__ and _emph_",
            "Some bold and italic and under and emph",
        ),
        ("Use `uv run` here", "Use uv run here"),
        ("A [link](https://example.invalid) and ![pic](x.png)", "A link and pic"),
        ("~~gone~~ stays", "gone stays"),
        ("- list item", "- list item"),
        ("> quoted", "> quoted"),
        ("cron * * * * * stays", "cron * * * * * stays"),
        ("snake_case_name stays", "snake_case_name stays"),
    ],
)
def test_strip_markdown_line(line: str, shown: str | None) -> None:
    assert strip_markdown_line(line) == shown


def test_argument_preview_prefers_the_main_argument() -> None:
    assert argument_preview({"limit": 3, "path": "a.txt"}) == "a.txt"
    assert argument_preview({"other": "  spaced\n out  "}) == "spaced out"
    assert argument_preview({"n": 1}) == ""
    assert len(argument_preview({"command": "x" * 500})) == 60


def test_format_elapsed() -> None:
    assert format_elapsed(400) == "0.4s"
    assert format_elapsed(12_340) == "12.3s"
    assert format_elapsed(125_000) == "2m 05s"
