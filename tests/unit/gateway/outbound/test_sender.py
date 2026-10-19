"""P15-17: the outbound sender and dead targets."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.scope import Scope, make_scope
from indus.gateway.core.send import SendResult
from indus.gateway.core.sent_index import SentIndex
from indus.gateway.outbound.dead_targets import DeadTargets
from indus.gateway.outbound.filters import SILENT_NOTICE
from indus.gateway.outbound.sender import DeliveryReport, FinalReply, OutboundSender, ReplyToMode
from tests.fakes.clock import FakeClock
from tests.unit.gateway.outbound.recording import RecordingAdapter, fail, recording_adapter

THREE_PIECES = " ".join(f"word{i:02d}" for i in range(24))  # ~170 chars: three pieces at 80


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime.now(UTC).replace(microsecond=0))


@pytest.fixture
def out_scope(tmp_path: Path, clock: FakeClock) -> Scope:
    profile = tmp_path / "root" / "profiles" / "work"
    profile.mkdir(parents=True)
    return make_scope(profile, env={"HOME": str(tmp_path / "userhome")}, clock=clock)


def _deliver(sender: OutboundSender, reply: FinalReply, chat_id: str = "c1") -> DeliveryReport:
    return asyncio.run(sender.deliver(chat_id, reply))


def _sender(scope: Scope, adapter: RecordingAdapter, **kwargs: Any) -> OutboundSender:
    return OutboundSender(adapter, scope=scope, **kwargs)


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("off", [None, None, None]),
        ("first", ["in1", None, None]),
        ("all", ["in1", "in1", "in1"]),
    ],
)
def test_reply_to_mode_table(
    mode: ReplyToMode, expected: list[str | None], out_scope: Scope
) -> None:
    adapter = recording_adapter(max_len=80)
    sender = _sender(out_scope, adapter, reply_to_mode=mode)
    report = _deliver(sender, FinalReply(THREE_PIECES, reply_to="in1", thread_id="t7"))
    assert report.outcome == "sent" and len(report.message_ids) == 3
    assert [op.reply_to for op in adapter.ops] == expected
    assert all(op.meta == {"thread_id": "t7", "final": True} for op in adapter.ops)


def test_redaction_applies_before_chunking(out_scope: Scope) -> None:
    secret = "ghp_" + "Q7w" * 12
    text = "a" * 60 + " " + secret + " " + "b" * 60
    adapter = recording_adapter(max_len=64)
    _deliver(_sender(out_scope, adapter), FinalReply(text))
    sent = "".join(op.text for op in adapter.ops)
    assert len(adapter.ops) > 1
    assert secret[:12] not in sent and secret[-12:] not in sent


def test_silence_rules(out_scope: Scope) -> None:
    adapter = recording_adapter()
    sender = _sender(out_scope, adapter)
    assert _deliver(sender, FinalReply("NO_REPLY", internal=True)).outcome == "silent"
    assert _deliver(sender, FinalReply("[SILENT]", reply_expected=False)).outcome == "silent"
    assert adapter.ops == []
    assert _deliver(sender, FinalReply("NO_REPLY", reply_expected=True)).outcome == "sent"
    assert adapter.ops[-1].text == SILENT_NOTICE
    _deliver(sender, FinalReply("NO_REPLY", failed=True))
    assert adapter.ops[-1].text == "NO_REPLY"
    _deliver(sender, FinalReply("I chose NO_REPLY.", internal=True))
    assert adapter.ops[-1].text == "I chose NO_REPLY."


def test_empty_reply_sends_nothing(out_scope: Scope) -> None:
    adapter = recording_adapter()
    assert _deliver(_sender(out_scope, adapter), FinalReply("   ")).outcome == "empty"
    assert adapter.ops == []


def test_dead_target_skipped_then_revived(out_scope: Scope) -> None:
    path = out_scope.home / "gateway" / "dead_targets.json"
    dead = DeadTargets(path)
    adapter = recording_adapter(name="telegram")
    sender = _sender(out_scope, adapter, dead_targets=dead)
    adapter.send_results.append(fail("forbidden", "bot was blocked by the user"))
    assert _deliver(sender, FinalReply("cron output", unsolicited=True)).outcome == "failed"
    assert dead.is_dead("telegram", "c1")
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert DeadTargets(path).is_dead("telegram", "c1")  # persisted

    assert _deliver(sender, FinalReply("cron again", unsolicited=True)).outcome == "skipped_dead"
    assert len(adapter.ops) == 1
    # the user writes again: a reply proves the chat alive and clears the flag
    assert _deliver(sender, FinalReply("welcome back", reply_to="in9")).outcome == "sent"
    assert not dead.is_dead("telegram", "c1")
    assert _deliver(sender, FinalReply("cron output", unsolicited=True)).outcome == "sent"


@pytest.mark.parametrize(
    ("result", "dead"),
    [
        (fail("forbidden", "Forbidden: bot was kicked"), True),
        (fail("not_found", "Bad Request: chat not found"), True),
        (fail("not_found", "Bad Request: message thread not found"), False),
        (fail("not_found", "chat not found; message to reply not found"), False),
        (fail("transient", "connect timeout"), False),
        (fail("rate_limited", "Too Many Requests"), False),
    ],
)
def test_only_whole_chat_failures_mark_a_target(
    result: SendResult, dead: bool, out_scope: Scope
) -> None:
    targets = DeadTargets(None)
    targets.note("slack", "C1", result)
    assert targets.is_dead("slack", "C1") is dead


def test_corrupt_dead_target_file_starts_empty(tmp_path: Path) -> None:
    path = tmp_path / "dead.json"
    path.write_text("{not json", encoding="utf-8")
    targets = DeadTargets(path)
    assert targets.entries() == {}
    assert targets.mark("discord", "42", "x" * 500)
    assert len(targets.entries()["discord:42"]["reason"]) == 200


def test_unwritable_dead_target_file_keeps_memory(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("")
    targets = DeadTargets(blocker / "sub" / "dead.json")  # parent is a file: cannot be created
    assert targets.mark("telegram", "7")
    assert targets.is_dead("telegram", "7")


def test_bad_format_piece_is_resent_plain_once(out_scope: Scope) -> None:
    adapter = recording_adapter(markup="markdownv2")
    adapter.send_results.append(fail("bad_format", "can't parse entities"))
    report = _deliver(_sender(out_scope, adapter), FinalReply("Version **1.2** is out!"))
    assert report.outcome == "sent"
    assert [op.text for op in adapter.ops] == ["Version *1\\.2* is out\\!", "Version 1.2 is out!"]


def test_partial_failure_reports_only_the_undelivered_pieces(out_scope: Scope) -> None:
    adapter = recording_adapter(max_len=80)
    # a long rate limit is handed back at once (no inline sleep, R06.3): the delivery stops
    flood = fail("rate_limited", "Too Many Requests: retry after 600", retry_after=600)
    adapter.send_results.extend([SendResult(ok=True), flood])
    index = SentIndex()
    report = _deliver(_sender(out_scope, adapter, sent_index=index), FinalReply(THREE_PIECES))
    assert report.outcome == "failed" and report.failure is not None
    assert len(report.message_ids) == 1
    assert len(report.undelivered) == 2
    assert report.undelivered == [op.text for op in adapter.ops[1:]] + [report.undelivered[-1]]
    assert len(index) == 1 and index.is_own("stub", "c1", report.message_ids[0])


def test_sent_index_records_plain_text(out_scope: Scope) -> None:
    adapter = recording_adapter(markup="markdownv2")
    index = SentIndex()
    _deliver(_sender(out_scope, adapter, sent_index=index), FinalReply("Done. **Yes**"))
    assert len(index) == 1 and index.lookup("stub", "c1", "m1") == "Done. Yes"


def test_media_tags_are_sent_natively_or_refused_with_a_notice(
    out_scope: Scope, clock: FakeClock
) -> None:
    image = out_scope.home / "cache" / "images" / "chart.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"png")
    old = clock.now().timestamp() - 86_400
    os.utime(image, (old, old))
    adapter = recording_adapter()
    reply = FinalReply(f"Here you go:\nMEDIA:{image}\nMEDIA:/etc/passwd", reply_to="in1")
    report = _deliver(_sender(out_scope, adapter), reply)
    assert [(op.op, op.text) for op in adapter.ops] == [
        ("send", "Here you go:"),
        ("image", os.path.realpath(image)),
        ("send", "Could not attach passwd: not allowed."),
    ]
    assert adapter.ops[0].reply_to == "in1"
    assert report.notices == ["Could not attach passwd: not allowed."]


def test_ephemeral_notice_is_deleted_after_ttl(out_scope: Scope, clock: FakeClock) -> None:
    adapter = recording_adapter(clock=clock)

    async def main() -> None:
        sender = _sender(out_scope, adapter, ephemeral_ttl=30.0)
        await sender.deliver("c1", FinalReply("Model switched.", ephemeral=True))
        await sender.deliver("c1", FinalReply("A normal reply."))
        await clock.run_until_idle()
        await clock.advance_and_settle(29)
        assert [op.op for op in adapter.ops] == ["send", "send"]
        await clock.advance_and_settle(1)
        await sender.aclose()

    asyncio.run(main())
    assert [(op.op, op.message_id) for op in adapter.ops] == [
        ("send", "m1"),
        ("send", "m2"),
        ("delete", "m1"),
    ]


def test_ephemeral_needs_delete_support_and_a_ttl(out_scope: Scope, clock: FakeClock) -> None:
    adapter = recording_adapter(clock=clock, supports_delete=False)

    async def main() -> None:
        sender = _sender(out_scope, adapter, ephemeral_ttl=30.0)
        await sender.deliver("c1", FinalReply("note", ephemeral=True))
        await sender.deliver("c1", FinalReply("note", ephemeral=True, ttl=0))
        await clock.advance_and_settle(60)
        await sender.aclose()

    asyncio.run(main())
    assert [op.op for op in adapter.ops] == ["send", "send"]
