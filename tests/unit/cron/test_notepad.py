"""Per-job notepad (P14-17): limits keep the store untouched, prompt block, cleared with the job."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from indus.cron.context import CronContext
from indus.cron.errors import NotepadLimit
from indus.cron.ledger import ExecutionsDB
from indus.cron.models import JobSpec
from indus.cron.notepad import MAX_JOB_BYTES, MAX_KEY_CHARS, MAX_VALUE_BYTES, Notepad
from indus.foundation.scope import Scope
from tests.fakes.agent import DictConfig
from tests.fakes.clock import FakeClock
from tests.fakes.pids import FakePids


@pytest.fixture
def cron_scope(scope: Scope) -> Scope:
    return scope.with_config(DictConfig({"timezone": "UTC"}))


@pytest.fixture
def notepad(cron_scope: Scope) -> Iterator[Notepad]:
    db = ExecutionsDB(cron_scope)
    yield Notepad(db)
    db.close()


def test_set_get_list_unset(notepad: Notepad, fake_clock: FakeClock) -> None:
    entry = notepad.set("job1", "cursor", "page=4")
    assert entry.updated_at == fake_clock.now()
    notepad.set("job1", "a-first", "1")
    notepad.set("job2", "cursor", "other job")
    assert notepad.get("job1", "cursor") == "page=4"
    assert notepad.get("job1", "missing") is None
    assert [(e.key, e.value) for e in notepad.list("job1")] == [
        ("a-first", "1"),
        ("cursor", "page=4"),
    ]
    fake_clock.advance(10)
    notepad.set("job1", "cursor", "page=5")
    assert notepad.get("job1", "cursor") == "page=5"
    assert notepad.list("job1")[1].updated_at == fake_clock.now()
    assert notepad.unset("job1", "cursor") is True
    assert notepad.unset("job1", "cursor") is False
    assert notepad.get("job2", "cursor") == "other job"


def test_value_limit(notepad: Notepad) -> None:
    notepad.set("job1", "k", "x" * MAX_VALUE_BYTES)  # exactly 16 KiB is fine
    notepad.set("job1", "keep", "original")
    with pytest.raises(NotepadLimit) as info:
        notepad.set("job1", "keep", "x" * (MAX_VALUE_BYTES + 1))
    assert info.value.code == "cron.notepad_limit"
    assert notepad.get("job1", "keep") == "original"  # unchanged
    with pytest.raises(NotepadLimit):
        notepad.set("job1", "new", "é" * (MAX_VALUE_BYTES // 2 + 1))  # bytes, not characters
    assert notepad.get("job1", "new") is None


def test_job_total_limit(notepad: Notepad) -> None:
    value = "v" * 16_000
    for i in range(4):
        notepad.set("job1", f"key{i}", value)

    def used() -> int:
        return sum(len(e.key) + len(e.value) for e in notepad.list("job1"))

    room = MAX_JOB_BYTES - used()
    before = [(e.key, e.value) for e in notepad.list("job1")]
    with pytest.raises(NotepadLimit, match="unset keys"):
        notepad.set("job1", "key4", "v" * room)  # the key's own bytes push it over
    assert [(e.key, e.value) for e in notepad.list("job1")] == before
    notepad.set("job1", "key4", "v" * (room - len("key4")))  # exactly at the limit
    assert used() == MAX_JOB_BYTES
    # Replacing a key counts only its new size.
    notepad.set("job1", "key0", "short")
    notepad.set("job1", "key5", "v" * (MAX_JOB_BYTES - used() - len("key5")))
    assert used() == MAX_JOB_BYTES
    # Another job has its own budget.
    notepad.set("job2", "big", value)


@pytest.mark.parametrize("key", ["", "   ", "a\nb", "tab\there", "x" * (MAX_KEY_CHARS + 1)])
def test_bad_keys(notepad: Notepad, key: str) -> None:
    with pytest.raises(NotepadLimit):
        notepad.set("job1", key, "v")
    assert notepad.list("job1") == []


def test_render_block(notepad: Notepad) -> None:
    assert notepad.render_block("job1") == ""
    notepad.set("job1", "cursor", "page=4")
    notepad.set("job1", "multi", "line one\nline two\n- fake: entry")
    block = notepad.render_block("job1")
    assert block.startswith("## Notes kept from earlier runs of this job\n")
    assert "indus cron notepad job1 set <key> <value>" in block
    assert "- cursor: page=4\n" in block
    assert "- multi: line one\n    line two\n    - fake: entry\n" in block
    assert block.count("\n- ") == 2  # a value cannot fake another entry


def test_render_block_survives_closed_db(cron_scope: Scope) -> None:
    db = ExecutionsDB(cron_scope)
    pad = Notepad(db)
    pad.set("job1", "k", "v")
    db.close()
    assert pad.render_block("job1") == ""


def test_removing_job_clears_notepad(cron_scope: Scope) -> None:
    ctx = CronContext.open(cron_scope, procs=FakePids())
    try:
        job = ctx.store.create(JobSpec(schedule="every 1h", prompt="watch"))
        other = ctx.store.create(JobSpec(schedule="every 1h", prompt="other"))
        ctx.notepad.set(job.id, "cursor", "7")
        ctx.notepad.set(other.id, "cursor", "9")
        ctx.store.remove(job.id)
        assert ctx.notepad.list(job.id) == []
        assert ctx.notepad.get(other.id, "cursor") == "9"
        assert ctx.health.gate() is True
        assert ctx.ledger.active() == []
    finally:
        ctx.close()
