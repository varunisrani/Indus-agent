"""Job model and store (P14-03): defaults, invariants, refs, locking, repair, curator source."""

from __future__ import annotations

import json
import logging
import multiprocessing
import os
import shutil
import stat
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from indus.cron.due import evaluate
from indus.cron.errors import (
    AmbiguousJobRef,
    CronStoreBusy,
    InvalidJob,
    JobNotFound,
    OneShotInPast,
    ScheduleError,
)
from indus.cron.grammar import CronSchedule, IntervalSchedule, OnceSchedule
from indus.cron.models import AUTHORED_FIELDS, SCHEDULER_FIELDS, CronJob, JobSpec, Origin
from indus.cron.store import CREATED_PAUSED_REASON, CronStore
from indus.foundation.clock import SystemClock
from indus.foundation.fsio import FileLock
from indus.foundation.scope import Scope, make_scope
from indus.learning import curator
from indus.learning.curator.lifecycle import apply_transitions
from indus.service.cron_refs import CronSkillReferences, register_cron_references
from tests.fakes.agent import DictConfig
from tests.fakes.clock import FakeClock
from tests.unit.learning.curator._helpers_f import (
    add_skill,
    clean_registries,  # noqa: F401  (autouse fixture)
    record,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "cron"


class NoSlots:
    def slot_attempted(self, job_id: str, scheduled_for: datetime) -> bool:
        return False


@pytest.fixture
def cron_scope(scope: Scope) -> Scope:
    return scope.with_config(DictConfig({"timezone": "UTC"}))


@pytest.fixture
def store(cron_scope: Scope) -> CronStore:
    return CronStore(cron_scope)


def spec(schedule: str = "every 30m", **fields: Any) -> JobSpec:
    fields.setdefault("prompt", "check the build")
    return JobSpec.model_validate({"schedule": schedule, **fields})


def raw_file(store: CronStore) -> dict[str, object]:
    return json.loads(store.paths.jobs_file.read_text(encoding="utf-8"))


# -- create -------------------------------------------------------------------------------------


def test_create_recurring_defaults(store: CronStore, fake_clock: FakeClock) -> None:
    job = store.create(spec())
    now = fake_clock.now()
    assert len(job.id) == 12 and int(job.id, 16) >= 0
    assert job.name == "check the build"
    assert job.deliver == "local"
    assert job.repeat.times is None and job.repeat.completed == 0
    assert job.state == "scheduled" and job.enabled
    assert job.next_run_at == now + timedelta(minutes=30)
    assert job.created_at == now
    assert isinstance(job.schedule, IntervalSchedule)
    assert job.schedule_display == "every 30m"
    assert [j.id for j in store.load()] == [job.id]
    on_disk = raw_file(store)
    assert on_disk["version"] == 1


def test_create_with_origin_delivers_to_origin(store: CronStore) -> None:
    job = store.create(spec(origin=Origin(platform="telegram", chat_id="-100", thread_id="7")))
    assert job.deliver == "origin" and job.origin is not None
    explicit = store.create(spec(deliver="local", origin=Origin(platform="x", chat_id="1")))
    assert explicit.deliver == "local"


def test_once_defaults_to_one_run(store: CronStore, fake_clock: FakeClock) -> None:
    job = store.create(spec("in 2h"))
    assert job.repeat.times == 1
    assert job.next_run_at == fake_clock.now() + timedelta(hours=2)
    assert not job.recurring


@pytest.mark.parametrize(("ago", "ok"), [(119, True), (121, False)])
def test_create_once_in_past(store: CronStore, fake_clock: FakeClock, ago: int, ok: bool) -> None:
    when = (fake_clock.now() - timedelta(seconds=ago)).isoformat()
    if ok:
        assert store.create(spec(when)).next_run_at is not None
    else:
        with pytest.raises(OneShotInPast):
            store.create(spec(when))
        assert store.load() == []


def test_repeat_zero_is_forever(store: CronStore) -> None:
    assert store.create(spec(repeat=0)).repeat.times is None
    assert store.create(spec(repeat=3)).repeat.times == 3


@pytest.mark.parametrize(
    ("fields", "match"),
    [
        ({"prompt": "  "}, "prompt, a script or at least one skill"),
        ({"prompt": None, "no_agent": True}, "no_agent needs a script"),
        ({"monitor_script": "a.sh", "monitor_url": "https://example.test"}, "exclude each other"),
        ({"monitor_url": "https://example.test", "no_agent": True, "script": "a.sh"}, "no_agent"),
        ({"paused_reason": "why"}, "paused=True"),
    ],
)
def test_create_invariants(store: CronStore, fields: dict[str, Any], match: str) -> None:
    with pytest.raises(InvalidJob, match=match):
        store.create(spec(**fields))
    assert store.load() == []


def test_payload_from_skills_or_script(store: CronStore) -> None:
    by_skill = store.create(spec(prompt=None, skills=[" digest ", "digest", ""]))
    assert by_skill.skills == ["digest"] and by_skill.name == "digest"
    by_script = store.create(spec(prompt=None, script="watch.sh", no_agent=True))
    assert by_script.name == "watch.sh" and by_script.no_agent


def test_script_check_hook(cron_scope: Scope) -> None:
    def refuse(script: str) -> None:
        if " " in script:
            raise InvalidJob(f"{script!r} looks like a command line")

    store = CronStore(cron_scope, script_check=refuse)
    with pytest.raises(InvalidJob, match="command line"):
        store.create(spec(script="ls -la"))
    assert store.create(spec(script="ok.sh")).script == "ok.sh"


def test_bad_schedule_refused(store: CronStore) -> None:
    with pytest.raises(ScheduleError):
        store.create(spec("now and then"))


def test_created_paused(store: CronStore, fake_clock: FakeClock) -> None:
    job = store.create(spec(paused=True))
    assert job.enabled is False and job.state == "paused" and job.next_run_at is None
    assert job.paused_at == fake_clock.now()
    assert job.paused_reason == CREATED_PAUSED_REASON
    custom = store.create(spec(paused=True, paused_reason="needs review"))
    assert custom.paused_reason == "needs review"
    # A paused record is never due, however late it gets.
    fake_clock.advance(10 * 86400)
    for loaded in store.load():
        decision = evaluate(loaded, now=fake_clock.now(), tz=UTC, slots=NoSlots())
        assert decision.fire is False


def test_half_paused_record_never_fires(store: CronStore, fake_clock: FakeClock) -> None:
    job = store.create(spec())
    # Hand edit: enabled stays true but a pause stamp is set.
    store.patch(job.id, paused_at=fake_clock.now())
    fake_clock.advance(3600)
    loaded = store.resolve(job.id)
    assert loaded.paused and not loaded.runnable and loaded.display_state == "paused"
    assert evaluate(loaded, now=fake_clock.now(), tz=UTC, slots=NoSlots()).fire is False


# -- references ---------------------------------------------------------------------------------


def test_resolve_by_id_and_name(store: CronStore) -> None:
    a = store.create(spec(name="Nightly Report"))
    assert store.resolve(a.id).id == a.id
    assert store.resolve("nightly report").id == a.id
    assert store.resolve("  NIGHTLY REPORT ").id == a.id
    with pytest.raises(JobNotFound):
        store.resolve("weekly report")
    with pytest.raises(JobNotFound):
        store.resolve("")


def test_exact_id_wins_over_name(store: CronStore) -> None:
    a = store.create(spec(name="first"))
    b = store.create(spec(name=a.id))  # a job named like another job's id
    assert store.resolve(a.id).id == a.id
    assert store.resolve(b.id).id == b.id


def test_ambiguous_name_refused_with_ids(store: CronStore) -> None:
    a = store.create(spec(name="Digest"))
    b = store.create(spec(name="digest"))
    with pytest.raises(AmbiguousJobRef) as info:
        store.resolve("DIGEST")
    assert set(info.value.candidates) == {a.id, b.id}
    assert a.id in str(info.value) and b.id in str(info.value)
    with pytest.raises(AmbiguousJobRef):
        store.pause("digest")


# -- update / pause / resume / remove -----------------------------------------------------------


def test_update_keeps_run_state(store: CronStore, fake_clock: FakeClock) -> None:
    job = store.create(spec())
    store.patch(job.id, last_status="error", failure_streak=2, last_error="boom")
    updated = store.update(job.id, prompt="check the build twice", skills=["ci"])
    assert updated.prompt == "check the build twice" and updated.skills == ["ci"]
    assert updated.failure_streak == 2 and updated.last_status == "error"
    assert updated.next_run_at == job.next_run_at  # schedule untouched → not re-planned


def test_update_skills_empty_clears(store: CronStore) -> None:
    job = store.create(spec(skills=["a", "b"]))
    assert store.update(job.id, skills=[]).skills == []
    with pytest.raises(InvalidJob):
        store.update(job.id, prompt="")  # would leave no payload


def test_update_schedule_replans(store: CronStore, fake_clock: FakeClock) -> None:
    job = store.create(spec())
    store.patch(job.id, quota_hold_until=fake_clock.now() + timedelta(hours=5))
    updated = store.update(job.id, schedule="every 2h")
    assert updated.schedule_display == "every 2h"
    assert updated.next_run_at == fake_clock.now() + timedelta(hours=2)
    assert updated.quota_hold_until is None
    once = store.update(job.id, schedule="in 10m")
    assert once.repeat.times == 1 and isinstance(once.schedule, OnceSchedule)
    back = store.update(job.id, schedule="daily at 9am")
    assert back.repeat.times is None and isinstance(back.schedule, CronSchedule)
    with pytest.raises(OneShotInPast):
        store.update(job.id, schedule="2020-01-01T00:00")


def test_update_refuses_run_state_and_unknown_fields(store: CronStore) -> None:
    job = store.create(spec())
    with pytest.raises(InvalidJob, match="cannot be edited"):
        store.update(job.id, failure_streak=0)
    with pytest.raises(InvalidJob, match="cannot be edited"):
        store.update(job.id, id="other")


def test_update_paused_job_schedule_not_planned(store: CronStore) -> None:
    job = store.create(spec(paused=True))
    assert store.update(job.id, schedule="every 5m").next_run_at is None


def test_pause_and_resume_keeps_missed_slot(store: CronStore, fake_clock: FakeClock) -> None:
    job = store.create(spec("every 1h"))
    slot = job.next_run_at
    paused = store.pause(job.id, reason="travelling")
    assert paused.enabled is False and paused.state == "paused"
    assert paused.paused_reason == "travelling" and paused.next_run_at == slot
    fake_clock.advance(2 * 86400)
    resumed = store.resume(job.id)
    assert resumed.enabled and resumed.state == "scheduled" and resumed.paused_at is None
    assert resumed.next_run_at == slot  # still in the past: one catch-up or a logged skip
    decision = evaluate(resumed, now=fake_clock.now(), tz=UTC, slots=NoSlots())
    assert decision.fire and decision.lateness == "catch_up"


def test_resume_future_slot_replanned(store: CronStore, fake_clock: FakeClock) -> None:
    job = store.create(spec("every 1h"))
    store.pause(job.id)
    fake_clock.advance(600)
    resumed = store.resume(job.id)
    assert resumed.next_run_at == fake_clock.now() + timedelta(hours=1)


def test_resume_expired_oneshot_refused(store: CronStore, fake_clock: FakeClock) -> None:
    job = store.create(spec("in 5m", paused=True))
    fake_clock.advance(3600)
    with pytest.raises(OneShotInPast):
        store.resume(job.id)


def test_remove_runs_hooks(store: CronStore) -> None:
    seen: list[str] = []
    store.add_remove_hook(seen.append)
    store.add_remove_hook(lambda _id: 1 / 0)  # a failing hook does not undo the removal
    job = store.create(spec(name="gone soon"))
    keep = store.create(spec(name="stays"))
    removed = store.remove("gone soon")
    assert removed.id == job.id and seen == [job.id]
    assert [j.id for j in store.load()] == [keep.id]


def test_patch_scheduler_fields_only(store: CronStore, fake_clock: FakeClock) -> None:
    job = store.create(spec())
    out = store.patch(job.id, last_run_at=fake_clock.now(), repeat={"times": None, "completed": 1})
    assert out is not None and out.repeat.completed == 1
    with pytest.raises(InvalidJob, match="use update"):
        store.patch(job.id, prompt="sneaky")
    with pytest.raises(InvalidJob):
        store.patch(job.id, failure_streak=-1)
    assert store.patch("000000000000", last_status="ok") is None


def test_field_split_is_complete() -> None:
    fields = set(CronJob.model_fields)
    assert AUTHORED_FIELDS | SCHEDULER_FIELDS | {"id", "created_at", "repeat"} == fields
    assert not AUTHORED_FIELDS & SCHEDULER_FIELDS


def test_import_merge_keeps_run_state(store: CronStore, fake_clock: FakeClock) -> None:
    job = store.create(spec())
    store.patch(job.id, last_status="ok", failure_streak=1, repeat={"completed": 7})
    merged = store.import_definitions(
        {
            job.id: {"prompt": "new words", "repeat": {"times": 9, "completed": 0}},
            "c0ffee000001": {"prompt": "brand new", "schedule": "every 2h"},
        },
        paused_reason="imported, waiting for approval",
    )
    by_id = {j.id: j for j in merged}
    kept = by_id[job.id]
    assert kept.prompt == "new words" and kept.repeat.times == 9 and kept.repeat.completed == 7
    assert kept.last_status == "ok" and kept.failure_streak == 1
    assert kept.next_run_at == job.next_run_at
    new = by_id["c0ffee000001"]
    assert new.enabled is False and new.state == "paused" and new.next_run_at is None
    assert new.paused_reason == "imported, waiting for approval"
    replanned = store.import_definitions({job.id: {"schedule": "every 5m"}}, paused_reason="x")[0]
    assert replanned.next_run_at == fake_clock.now() + timedelta(minutes=5)


# -- file handling ------------------------------------------------------------------------------


def test_unknown_keys_survive_rewrite(store: CronStore) -> None:
    store.ensure_dirs()
    shutil.copy(FIXTURES / "jobs_valid.json", store.paths.jobs_file)
    jobs = store.load()
    assert [j.label for j in jobs] == ["Build check", "Inbox digest"]
    assert jobs[1].paused and jobs[1].display_state == "paused"
    store.create(spec())
    first = raw_file(store)["jobs"][0]  # type: ignore[index]
    assert first["future_field_from_newer_build"] == {"kept": True}


def test_repair_drops_junk_and_warns_types_only(
    store: CronStore, caplog: pytest.LogCaptureFixture
) -> None:
    store.ensure_dirs()
    shutil.copy(FIXTURES / "jobs_corrupt.json", store.paths.jobs_file)
    with caplog.at_level(logging.WARNING, logger="indus.cron.store"):
        jobs = store.load()
    assert [j.id for j in jobs] == ["111111111111", "222222222222"]
    first, second = jobs
    assert first.repeat.completed == 0 and first.repeat.times is None
    assert first.failure_streak == 0
    assert second.repeat.completed == 0 and second.repeat.times == 2
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    text = warnings[0]
    assert "str" in text and "int" in text and "null" in text and "list" in text
    assert "repeat.completed" in text and "failure_streak" in text
    for secret in ("private words", "secret-ish", "three", "lots", "sometimes", "escape"):
        assert secret not in text
    # The repair was written once and the dropped records kept aside.
    on_disk = raw_file(store)
    assert [j["id"] for j in on_disk["jobs"]] == ["111111111111", "222222222222"]  # type: ignore[index]
    rejected = json.loads(store.paths.rejected_file.read_text(encoding="utf-8"))
    assert {r["id"] for r in rejected} == {"333333333333", "../../escape"}
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="indus.cron.store"):
        store.load()
    assert not caplog.records  # nothing left to repair


def test_legacy_bare_list_and_naive_times(store: CronStore) -> None:
    store.ensure_dirs()
    shutil.copy(FIXTURES / "jobs_legacy_list.json", store.paths.jobs_file)
    (job,) = store.load()
    assert job.created_at == datetime(2026, 1, 1, 8, 0, tzinfo=UTC)
    assert job.next_run_at == datetime(2026, 1, 2, 9, 0, tzinfo=UTC)
    assert isinstance(raw_file(store), dict)


@pytest.mark.parametrize("content", ["{not json", '{"jobs": {"a": 1}}', '"text"'])
def test_unreadable_file_left_alone(store: CronStore, content: str) -> None:
    store.ensure_dirs()
    store.paths.jobs_file.write_text(content, encoding="utf-8")
    with pytest.raises(InvalidJob):
        store.load()
    with pytest.raises(InvalidJob):
        store.create(spec())
    assert store.paths.jobs_file.read_text(encoding="utf-8") == content


def test_empty_file_is_empty_store(store: CronStore) -> None:
    store.ensure_dirs()
    store.paths.jobs_file.write_text("", encoding="utf-8")
    assert store.load() == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_permissions(store: CronStore) -> None:
    store.create(spec())
    assert stat.S_IMODE(store.paths.cron_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.paths.jobs_file.stat().st_mode) == 0o600


def test_transactions_do_not_nest(store: CronStore) -> None:
    with store.transaction(), pytest.raises(RuntimeError), store.transaction():
        pass


def test_failed_transaction_writes_nothing(store: CronStore) -> None:
    store.create(spec())
    before = store.paths.jobs_file.read_bytes()
    with pytest.raises(ZeroDivisionError), store.transaction() as jobs:
        jobs.clear()
        _ = 1 / 0
    assert store.paths.jobs_file.read_bytes() == before


def test_lock_wait_then_busy(cron_scope: Scope, fake_clock: FakeClock) -> None:
    store = CronStore(cron_scope, lock_wait=fake_clock.advance)
    store.ensure_dirs()
    held = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with FileLock(store.paths.lock_file, timeout=1):
            held.set()
            release.wait(10)

    thread = threading.Thread(target=holder)
    thread.start()
    try:
        assert held.wait(5)
        start = fake_clock.monotonic()
        with pytest.raises(CronStoreBusy) as info:
            store.create(spec())
        assert fake_clock.monotonic() - start >= 30
        assert info.value.code == "cron.store_busy"
        assert "30 s" in str(info.value)
    finally:
        release.set()
        thread.join()
    assert store.create(spec()).id  # the lock is free again


def _writer_process(home: str, count: int, tag: str) -> None:
    scope = make_scope(Path(home), env={"INDUS_HOME": home}, clock=SystemClock())
    store = CronStore(scope.with_config(DictConfig({"timezone": "UTC"})))
    for i in range(count):
        store.create(spec(name=f"{tag}-{i}"))


@pytest.mark.skipif(sys.platform == "win32", reason="fork-free spawn is slow on Windows")
def test_two_processes_never_lose_jobs(cron_scope: Scope) -> None:
    ctx = multiprocessing.get_context("spawn")
    home = str(cron_scope.home)
    procs = [ctx.Process(target=_writer_process, args=(home, 15, tag)) for tag in ("a", "b")]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(120)
        assert proc.exitcode == 0
    names = sorted(j.name or "" for j in CronStore(cron_scope).load())
    assert len(names) == 30
    assert names == sorted([f"a-{i}" for i in range(15)] + [f"b-{i}" for i in range(15)])


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() != 0, reason="needs root")
def test_root_rewrite_restores_owner(store: CronStore) -> None:
    store.create(spec())
    os.chown(store.paths.jobs_file, 4242, 4242)
    store.create(spec())
    st = store.paths.jobs_file.stat()
    assert (st.st_uid, st.st_gid) == (4242, 4242)


# -- curator reference source (R07.8) -----------------------------------------------------------


def test_curator_skips_skill_of_paused_job(cron_scope: Scope) -> None:
    store = CronStore(cron_scope)
    folder = add_skill(cron_scope, "nightly-report", created_days=90, active_days=90, use_count=1)
    store.create(spec(prompt=None, skills=["nightly-report"], paused=True))
    undo = register_cron_references(store)
    try:
        out = apply_transitions(cron_scope)
    finally:
        undo()
    assert out.referenced == 1 and folder.exists()
    assert record(cron_scope, "nightly-report")["state"] == "active"


def test_curator_archives_unreferenced_skill(cron_scope: Scope) -> None:
    store = CronStore(cron_scope)
    folder = add_skill(cron_scope, "orphan", created_days=90, active_days=90, use_count=1)
    store.create(spec(skills=["something-else"]))
    undo = register_cron_references(store)
    try:
        out = apply_transitions(cron_scope)
    finally:
        undo()
    assert out.archived == 1 and not folder.exists()


def test_rename_rewrites_job_skills(cron_scope: Scope, fake_clock: FakeClock) -> None:
    store = CronStore(cron_scope)
    a = store.create(spec(skills=["old-name", "keep"]))
    b = store.create(spec(prompt=None, skills=["gone"]))
    c = store.create(spec(skills=["unrelated"]))
    undo = register_cron_references(store)
    try:
        results = curator.rewrite_references({"old-name": "new-name"}, ["gone"])
    finally:
        undo()
    (result,) = results
    assert result.source == "cron" and result.error is None
    assert any("old-name → new-name" in line for line in result.changes)
    assert store.resolve(a.id).skills == ["new-name", "keep"]
    left = store.resolve(b.id)
    assert left.skills == [] and left.paused and left.paused_reason
    assert store.resolve(c.id).skills == ["unrelated"]
    assert store.rename_skill("keep", "kept") == (f"job {a.label} ({a.id}): keep → kept",)
    adapter = CronSkillReferences(store)
    assert adapter.name == "cron"
    assert adapter.referenced_skills() == {"new-name", "kept", "unrelated"}
