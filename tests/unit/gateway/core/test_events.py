"""Inbound event model, command detection and reply context (P15-03)."""

from __future__ import annotations

import random

import pytest
from pydantic import ValidationError

from indus.gateway.core.events import (
    REPLY_QUOTE_MAX_BYTES,
    Attachment,
    InboundEvent,
    MessageKind,
    Source,
    model_text,
    normalise_dashes,
    parse_command,
    strip_attachment_refs,
    with_reply_context,
)
from indus.gateway.core.sent_index import SentIndex


def _event(text: str, **kw: object) -> InboundEvent:
    source = Source(platform="telegram", chat_id="c1", user_id="u1", user_name="Ana")
    return InboundEvent.model_validate({"source": source, "text": text, **kw})


@pytest.mark.parametrize(
    ("text", "name", "args"),
    [
        ("/help", "help", ""),
        ("/Help@MyBot args", "help", "args"),
        ("/NEW", "new", ""),
        ("  /status  ", "status", ""),
        ("/model\tgpt-x --once", "model", "gpt-x --once"),
        ("/model\nsecond line", "model", "second line"),
        ("/bg run \u2014verbose \u2013q", "bg", "run --verbose -q"),
        ("/bg \u2014\u2014all", "bg", "--all"),
        ("@image:/tmp/a.png\n/new", "new", ""),
        (
            "@IMAGE:/tmp/a b.png\n@file:C:\\Users\\Jo Do\\x.txt\r\n\n  /resume work",
            "resume",
            "work",
        ),
        ("@url:https://example.com\n/approve session", "approve", "session"),
    ],
)
def test_commands_are_detected(text: str, name: str, args: str) -> None:
    event = _event(text)
    assert event.is_command
    assert event.command == name
    assert event.command_args == args
    assert parse_command(text) == (name, args)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "hello",
        "/etc/hosts",
        "/etc/hosts is broken",
        "//double",
        "/",
        "/ help",
        "/@bot",
        "path /usr/bin",
        "@image:/tmp/a.png",
        "@image:\n/new",  # an empty ref line is not a ref, so the text starts with "@"
        "see @file:/x\n/new",
    ],
)
def test_plain_text_is_not_a_command(text: str) -> None:
    event = _event(text)
    assert not event.is_command
    assert event.command is None
    assert event.command_args == ""


def test_untrusted_events_never_run_commands() -> None:
    event = _event("/stop", allow_control=False)
    assert not event.is_command and event.command is None
    assert parse_command("/stop") == ("stop", "")


def _random_text(rng: random.Random) -> str:
    alphabet = "ab /@\n\t-\u2014\u2013:XYZéé😀"
    pieces = []
    if rng.random() < 0.3:
        pieces.append(rng.choice(["@image:/p", "@file:x y", "@url:u", "@IMAGE:z"]) + "\n")
    pieces.append("".join(rng.choice(alphabet) for _ in range(rng.randint(0, 16))))
    return "".join(pieces)


def test_property_text_not_starting_with_slash_is_never_a_command() -> None:
    for seed in range(40):
        rng = random.Random(seed)
        for _ in range(50):
            text = _random_text(rng)
            parsed = parse_command(text)
            if not strip_attachment_refs(text).startswith("/"):
                assert parsed is None, text
            if parsed is not None:
                assert parsed.name and parsed.name == parsed.name.lower(), text
                assert "/" not in parsed.name and "@" not in parsed.name, text
                assert "\u2014" not in parsed.args and "\u2013" not in parsed.args, text


def test_dash_normalisation() -> None:
    assert normalise_dashes("a \u2014b \u2013c \u2014\u2014d") == "a --b -c --d"
    assert normalise_dashes("plain -- text") == "plain -- text"


def test_strip_attachment_refs_keeps_later_refs() -> None:
    assert strip_attachment_refs("@image:/a\n@url:b\nhello\n@file:c") == "hello\n@file:c"
    assert strip_attachment_refs("  hi") == "hi"


def test_models_are_frozen_and_strict() -> None:
    event = _event("hi", media=(Attachment(ref="/tmp/a.ogg", mime="audio/ogg"),))
    with pytest.raises(ValidationError):
        event.text = "changed"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        Source(platform="x", chat_id="1", colour="red")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        Source(platform="x", chat_id="1", chat_type="room")  # type: ignore[arg-type]
    assert event.kind is MessageKind.TEXT
    assert event.user_id == "u1" and event.user_name == "Ana"
    changed = event.model_copy(update={"text": "/new"})
    assert changed.command == "new" and event.text == "hi"


def test_reply_context_from_the_index_and_own_mark() -> None:
    index = SentIndex()
    index.record("telegram", "c1", "m9", "our earlier answer")
    event = _event("why?", reply_to_id="m9")
    filled = with_reply_context(event, index)
    assert filled.reply_to_text == "our earlier answer"
    assert filled.reply_to_is_own
    assert model_text(filled) == '[Replying to your earlier message: "our earlier answer"]\n\nwhy?'


def test_platform_reply_text_wins_and_unknown_ids_are_not_own() -> None:
    index = SentIndex()
    index.record("telegram", "c1", "m9", "index text")
    event = _event("ok", reply_to_id="m9", reply_to_text="platform text")
    assert with_reply_context(event, index).reply_to_text == "platform text"
    other = _event("ok", reply_to_id="m1", reply_to_text="theirs", reply_to_author_name="Bo")
    filled = with_reply_context(other, index)
    assert not filled.reply_to_is_own
    assert model_text(filled) == '[Replying to Bo: "theirs"]\n\nok'
    assert with_reply_context(_event("no reply"), index).reply_to_is_own is False
    assert model_text(_event("plain")) == "plain"
    # Another chat's ids never match.
    elsewhere = InboundEvent(source=Source(platform="telegram", chat_id="c2"), reply_to_id="m9")
    assert with_reply_context(elsewhere, index).reply_to_text is None


def test_quote_is_capped_on_a_character_boundary() -> None:
    long = "é" * REPLY_QUOTE_MAX_BYTES
    text = model_text(_event("q", reply_to_id="x", reply_to_text=long))
    quote = text.split('"')[1]
    assert len(quote.encode("utf-8")) <= REPLY_QUOTE_MAX_BYTES
    assert set(quote) == {"é"}
