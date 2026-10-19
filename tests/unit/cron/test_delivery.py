"""P14-13: deliverer registry, local delivery, masking, wrapper and the status split."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from indus.cron import bookkeeping, delivery, ports
from indus.cron.delivery import DeliveryReceipt, DeliveryRequest, deliver
from indus.cron.models import CronJob
from indus.foundation.config import loader
from indus.foundation.scope import Scope
from tests.unit.cron.world import record

FAKE_KEY = "sk-" + "proj" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4"


@pytest.fixture
def sent() -> Iterator[list[DeliveryRequest]]:
    seen: list[DeliveryRequest] = []

    async def fake(request: DeliveryRequest) -> DeliveryReceipt:
        seen.append(request)
        return DeliveryReceipt(ok=True, message_id="m1")

    undo = delivery.register_deliverer("telegram", fake)
    yield seen
    undo()


def output_doc(scope: Scope, job_id: str = "abc123abc123") -> Path:
    path = ports.output_dir(scope, job_id) / "2026-01-01T00-00-00Z.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# run\n", encoding="utf-8")
    return path


def job(**fields: Any) -> CronJob:
    fields.setdefault("name", "build check")
    return record(**fields)


def with_config(scope: Scope, text: str) -> Scope:
    path = loader.config_path(scope)
    path.write_text(text, encoding="utf-8")
    loader.invalidate(path)
    return scope.with_config(loader.load(scope))


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_local_delivery_reports_the_output_document(scope: Scope) -> None:
    doc = output_doc(scope)
    result = run(deliver(scope, job(), "all green", output_path=doc))
    assert result.status == "delivered"
    assert [o.target for o in result.outcomes] == ["local"]


def test_local_delivery_without_a_document_fails(scope: Scope) -> None:
    result = run(deliver(scope, job(), "all green", output_path=None))
    assert result.status == "delivery_failed"
    assert "output document" in (result.reason or "")


def test_origin_without_a_stored_origin_is_local(scope: Scope) -> None:
    result = run(deliver(scope, job(deliver="origin"), "hi", output_path=output_doc(scope)))
    assert result.status == "delivered"
    assert [o.target for o in result.outcomes] == ["local"]


def test_origin_resolves_to_the_stored_chat(scope: Scope, sent: list[DeliveryRequest]) -> None:
    origin = {"platform": "telegram", "chat_id": "-100", "thread_id": "7"}
    result = run(deliver(scope, job(deliver="origin,telegram:-100:7", origin=origin), "hi"))
    assert result.status == "delivered"
    assert len(sent) == 1  # origin and the same explicit chat collapse
    assert sent[0].target.render() == "telegram:-100:7"


def test_empty_payload_is_never_sent(scope: Scope, sent: list[DeliveryRequest]) -> None:
    for text in ("", "   \n  "):
        result = run(deliver(scope, job(deliver="telegram:1"), text))
        assert result.status == "suppressed"
    assert sent == []


def test_redactor_failure_sends_the_fixed_notice(
    scope: Scope, sent: list[DeliveryRequest], monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(text: str, scope: Scope) -> str:
        raise RuntimeError("masking exploded")

    monkeypatch.setattr(delivery, "REDACTOR", broken)
    result = run(deliver(scope, job(deliver="telegram:1"), f"key {FAKE_KEY}"))
    assert result.status == "delivered"
    assert sent[0].plain_text == delivery.REDACTION_FAILED_NOTICE
    assert FAKE_KEY not in sent[0].text


def test_secrets_are_masked_even_with_log_redaction_off(
    scope: Scope, sent: list[DeliveryRequest]
) -> None:
    scope = with_config(scope, "security:\n  redact_secrets: false\n")
    name = f"leaky {FAKE_KEY}"
    run(deliver(scope, job(deliver="telegram:1", name=name), f"found {FAKE_KEY} in the log"))
    assert FAKE_KEY not in sent[0].text
    assert FAKE_KEY not in sent[0].job_name


def test_wrapper_on_and_off(scope: Scope, sent: list[DeliveryRequest]) -> None:
    run(deliver(scope, job(deliver="telegram:1"), "the build is red"))
    framed = sent[-1].text
    assert framed.startswith("Scheduled job: build check [abc123abc123]")
    assert "the build is red" in framed and "does not see this message" in framed
    scope = with_config(scope, "cron:\n  wrap_response: false\n")
    run(deliver(scope, job(deliver="telegram:1"), "the build is red"))
    assert sent[-1].text == "the build is red"


def test_platform_without_a_deliverer_needs_the_gateway(scope: Scope) -> None:
    result = run(deliver(scope, job(deliver="discord:42"), "hi"))
    assert result.status == "delivery_failed"
    assert "needs the gateway" in (result.reason or "")
    all_result = run(deliver(scope, job(deliver="all"), "hi"))
    assert all_result.status == "delivery_failed"
    assert "`all`" in (all_result.reason or "")


def test_unknown_platform_in_a_stored_value_fails_and_others_still_send(
    scope: Scope, sent: list[DeliveryRequest]
) -> None:
    result = run(deliver(scope, job(deliver="myspace:1,telegram:5"), "hi"))
    assert result.status == "delivery_failed"
    assert "unknown_platform" in (result.reason or "")
    assert [r.target.render() for r in sent] == ["telegram:5"]


def test_failure_lane_uses_failure_deliver(scope: Scope, sent: list[DeliveryRequest]) -> None:
    j = job(deliver="local", failure_deliver="telegram:9")
    run(deliver(scope, j, "it broke", for_failure=True))
    assert [r.target.render() for r in sent] == ["telegram:9"] and sent[0].for_failure
    run(deliver(scope, j, "fine", output_path=output_doc(scope)))
    assert len(sent) == 1  # the success went to local only


def test_deliverer_errors_unverified_and_registry_undo(scope: Scope) -> None:
    async def raising(request: DeliveryRequest) -> DeliveryReceipt:
        raise ConnectionError(f"refused with {FAKE_KEY}")

    async def quiet(request: DeliveryRequest) -> DeliveryReceipt:
        return DeliveryReceipt(ok=True, verified=False)

    undo = delivery.register_deliverer("slack", raising)
    try:
        failed = run(deliver(scope, job(deliver="slack:C1"), "hi"))
        assert failed.status == "delivery_failed"
        assert "ConnectionError" in (failed.reason or "") and FAKE_KEY not in (failed.reason or "")
    finally:
        undo()
    undo = delivery.register_deliverer("slack", quiet)
    try:
        assert "slack" in delivery.registered_platforms()
        result = run(deliver(scope, job(deliver="slack:C1"), "hi"))
        assert result.status == "unverified" and result.unverified_targets == ("slack:C1",)
    finally:
        undo()
    assert delivery.deliverer_for("slack") is None
    with pytest.raises(ValueError):
        delivery.register_deliverer("origin", quiet)


def test_delivery_failure_leaves_the_failure_streak(scope: Scope) -> None:
    result = run(deliver(scope, job(deliver="discord:1"), "done"))
    assert result.failed
    j = job(failure_streak=2)
    outcome = bookkeeping.RunOutcome(succeeded=True, status="ok", delivery=result)
    settled = bookkeeping.settle_run(
        j,
        outcome,
        now=scope.clock.now(),
        settings=bookkeeping.Settings(),
        next_run=lambda s, b: None,
    )
    assert (
        settled.changes["failure_streak"] == 0
    )  # success resets; the delivery failure is not a run failure
    assert settled.changes["last_status"] == "delivery_failed"
    failed_run = bookkeeping.RunOutcome(succeeded=False, status="error", error="x", delivery=result)
    settled = bookkeeping.settle_run(
        j,
        failed_run,
        now=scope.clock.now(),
        settings=bookkeeping.Settings(),
        next_run=lambda s, b: None,
    )
    assert settled.changes["failure_streak"] == 3  # counted once, for the run


def test_attachments_only_from_the_job_output_folder(
    scope: Scope, sent: list[DeliveryRequest], tmp_path: Path
) -> None:
    inside = output_doc(scope)
    outside = tmp_path / "secret.txt"
    outside.write_text("x", encoding="utf-8")
    link = inside.parent / "escape.md"
    os.symlink(outside, link)
    env_file = inside.parent / ".env"
    env_file.write_text("A=1", encoding="utf-8")
    attachments: list[object] = [inside, outside, link, str(inside), env_file]
    result = run(deliver(scope, job(deliver="telegram:1"), "see attached", attachments=attachments))
    assert sent[0].attachments == (inside.resolve(),)
    assert result.status == "delivery_failed" and len(result.dropped_attachments) == 4


def test_local_notify_calls_the_notifier(scope: Scope) -> None:
    seen: list[tuple[str, str]] = []
    old = delivery.set_local_notifier(lambda title, line: seen.append((title, line)))
    try:
        run(deliver(scope, job(), "first line\nsecond", output_path=output_doc(scope)))
        assert seen == []
        scope = with_config(scope, "cron:\n  local_notify: true\n")
        run(deliver(scope, job(), "first line\nsecond", output_path=output_doc(scope)))
        assert seen == [("build check", "first line")]
    finally:
        delivery.set_local_notifier(old)
