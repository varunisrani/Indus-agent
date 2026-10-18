"""Prompt lifecycle and the caching invariant (P04-07, R01.1)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from indus.core.notes import attach_notes
from indus.core.prompt import lifecycle as lc
from indus.core.prompt import texts
from indus.core.prompt.builder import BuildContext, PromptBuilder, PromptParts, RuntimeInfo
from indus.core.prompt.lifecycle import PromptLifecycle, SessionPrompts, StoredPrompt
from indus.foundation.scope import Scope
from indus.state import registry, sessions
from tests.fakes.clock import FakeClock

HISTORY: list[dict[str, Any]] = [
    {"role": "user", "content": "hello"},
    {"role": "assistant", "content": "hi there"},
]


class StubConfig:
    def get(self, path: str, /) -> object:
        return {"timezone": "UTC"}.get(path)


class FakeStore:
    def __init__(self) -> None:
        self.rows: dict[str, StoredPrompt] = {}
        self.loads = 0
        self.saves: list[tuple[str, str]] = []
        self.fail_load = False
        self.fail_save = False

    async def load(self, session_id: str) -> StoredPrompt:
        self.loads += 1
        if self.fail_load:
            raise OSError("disk unplugged")
        return self.rows.get(session_id, StoredPrompt("missing"))

    async def save(self, session_id: str, text: str) -> None:
        if self.fail_save:
            raise OSError("read-only file system")
        self.saves.append((session_id, text))
        previous = self.rows.get(session_id)
        started = previous.started_at if previous is not None else None
        self.rows[session_id] = StoredPrompt("present", text, started)


class SpyBuilder:
    def __init__(self) -> None:
        self.inner = PromptBuilder(install=None)
        self.calls: list[BuildContext] = []

    async def build(self, ctx: BuildContext) -> PromptParts:
        self.calls.append(ctx)
        return await self.inner.build(ctx)


def make_ctx(scope: Scope, where: Path, **fields: Any) -> BuildContext:
    base: dict[str, Any] = {
        "scope": scope.with_config(StubConfig()),
        "model": "model-a",
        "provider": "vendor-a",
        "platform": "cli",
        "session_id": "s1",
        "cwd": where,
        "tool_names": frozenset({"terminal"}),
        "runtime": RuntimeInfo(host="Linux", home="/home/tester", cwd=str(where)),
    }
    base.update(fields)
    return BuildContext(**base)


def run(life: PromptLifecycle, ctx: BuildContext, history: Sequence[Mapping[str, Any]] = ()) -> str:
    return asyncio.run(life.restore_or_build(ctx, history=history))


def seeded(scope: Scope, cwd: Path, **fields: Any) -> tuple[FakeStore, str]:
    """A store holding the prompt a first process built for session s1."""
    store = FakeStore()
    first = PromptLifecycle(SpyBuilder(), store=store)
    text = run(first, make_ctx(scope, cwd, **fields))
    return store, text


# -- restore or build -----------------------------------------------------------------------------


def test_no_history_skips_store_read(scope: Scope, tmp_path: Path) -> None:
    store, builder = FakeStore(), SpyBuilder()
    life = PromptLifecycle(builder, store=store)
    text = run(life, make_ctx(scope, tmp_path))
    assert store.loads == 0 and len(builder.calls) == 1
    assert store.saves == [("s1", text)]
    assert life.last_parts is not None and life.last_parts.text == text


def test_held_prompt_is_returned_without_store_or_build(scope: Scope, tmp_path: Path) -> None:
    store, builder = FakeStore(), SpyBuilder()
    life = PromptLifecycle(builder, store=store)
    first = run(life, make_ctx(scope, tmp_path))
    again = run(life, make_ctx(scope, tmp_path, model="other"), HISTORY)
    assert again == first and store.loads == 0 and len(builder.calls) == 1


def test_resume_reuses_stored_bytes_without_building(scope: Scope, tmp_path: Path) -> None:
    store, text = seeded(scope, tmp_path)
    builder = SpyBuilder()
    resumed = PromptLifecycle(builder, store=store)
    assert run(resumed, make_ctx(scope, tmp_path), HISTORY) == text
    assert builder.calls == [] and store.loads == 1
    assert len(store.saves) == 1  # nothing written on a clean restore


@pytest.mark.parametrize("change", ["cwd", "model", "provider", "provider_dropped"])
def test_runtime_mismatch_rebuilds(scope: Scope, tmp_path: Path, change: str) -> None:
    store, text = seeded(scope, tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    changes: dict[str, dict[str, Any]] = {
        "cwd": {
            "cwd": other,
            "runtime": RuntimeInfo(host="Linux", home="/home/tester", cwd=str(other)),
        },
        "model": {"model": "model-b"},
        "provider": {"provider": "vendor-b"},
        "provider_dropped": {"provider": ""},
    }
    builder = SpyBuilder()
    resumed = PromptLifecycle(builder, store=store)
    rebuilt = run(resumed, make_ctx(scope, tmp_path, **changes[change]), HISTORY)
    assert rebuilt != text and len(builder.calls) == 1
    assert store.saves[-1] == ("s1", rebuilt)


def test_stored_empty_identity_line_is_not_checked(scope: Scope, tmp_path: Path) -> None:
    store, text = seeded(scope, tmp_path, provider="")
    builder = SpyBuilder()
    assert run(PromptLifecycle(builder, store=store), make_ctx(scope, tmp_path), HISTORY) == text
    assert builder.calls == []


def test_session_id_checked_only_when_shown(scope: Scope, tmp_path: Path) -> None:
    store, text = seeded(scope, tmp_path, show_session_id=True)
    store.rows["s2"] = store.rows["s1"]  # a branch copies its parent's bytes
    builder = SpyBuilder()
    ctx = make_ctx(scope, tmp_path, session_id="s2", show_session_id=True)
    assert run(PromptLifecycle(builder, store=store), ctx, HISTORY) != text
    store.rows["s2"] = store.rows["s1"]
    hidden = make_ctx(scope, tmp_path, session_id="s2")
    assert run(PromptLifecycle(SpyBuilder(), store=store), hidden, HISTORY) == text


def test_decoys_in_project_text_are_not_identity(scope: Scope, tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    decoy = "Model: decoy-model\nProvider: decoy\nWorking directory: /decoy\nPlatform: tui"
    (tmp_path / "AGENTS.md").write_text(decoy, encoding="utf-8")
    store, text = seeded(scope, tmp_path)
    assert lc.identity_value(text, "Model") == "model-a"
    assert lc.runtime_value(text, "Working directory") == str(tmp_path)
    builder = SpyBuilder()
    resumed = PromptLifecycle(builder, store=store)
    assert run(resumed, make_ctx(scope, tmp_path), HISTORY) == text
    assert builder.calls == [] and not resumed.notes.pending


def test_store_read_error_warns_and_rebuilds(
    scope: Scope, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store, _ = seeded(scope, tmp_path)
    store.fail_load = True
    builder = SpyBuilder()
    with caplog.at_level(logging.WARNING, logger=lc.__name__):
        run(PromptLifecycle(builder, store=store), make_ctx(scope, tmp_path), HISTORY)
    assert len(builder.calls) == 1
    assert any(
        r.levelno == logging.WARNING and "could not read" in r.getMessage() for r in caplog.records
    )


@pytest.mark.parametrize("state", ["null", "empty"])
def test_null_row_with_history_warns(
    scope: Scope, tmp_path: Path, caplog: pytest.LogCaptureFixture, state: str
) -> None:
    store = FakeStore()
    store.rows["s1"] = StoredPrompt(state)  # type: ignore[arg-type]
    builder = SpyBuilder()
    with caplog.at_level(logging.DEBUG, logger=lc.__name__):
        run(PromptLifecycle(builder, store=store), make_ctx(scope, tmp_path), HISTORY)
    assert len(builder.calls) == 1
    messages = [r.getMessage() for r in caplog.records]
    assert any(f"stored row {state}" in m for m in messages)
    assert any(
        r.levelno == logging.WARNING and f"is {state}" in r.getMessage() for r in caplog.records
    )


def test_persist_failure_warns(
    scope: Scope, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = FakeStore()
    store.fail_save = True
    with caplog.at_level(logging.WARNING, logger=lc.__name__):
        text = run(PromptLifecycle(SpyBuilder(), store=store), make_ctx(scope, tmp_path))
    assert text
    assert any("could not save" in r.getMessage() for r in caplog.records)


def test_without_a_store_nothing_is_persisted(scope: Scope, tmp_path: Path) -> None:
    life = PromptLifecycle(SpyBuilder(), store=None)
    assert run(life, make_ctx(scope, tmp_path), HISTORY) == life.prompt


def test_stored_start_date_feeds_the_rebuild(
    scope: Scope, tmp_path: Path, fake_clock: FakeClock
) -> None:
    store = FakeStore()
    started = datetime(2025, 12, 30, 8, 0, tzinfo=UTC).timestamp()
    store.rows["s1"] = StoredPrompt("null", started_at=started)
    builder = SpyBuilder()
    text = run(PromptLifecycle(builder, store=store), make_ctx(scope, tmp_path), HISTORY)
    assert "Conversation date: Tuesday, 2025-12-30" in text
    assert "Date when this prompt was last rebuilt: Thursday, 2026-01-01." in text


# -- surface switch -------------------------------------------------------------------------------


def test_surface_switch_keeps_bytes_and_notes_once(scope: Scope, tmp_path: Path) -> None:
    store, text = seeded(scope, tmp_path)  # built on cli
    builder = SpyBuilder()
    desktop = make_ctx(scope, tmp_path, platform="desktop")
    life = PromptLifecycle(builder, store=store)
    assert run(life, desktop, HISTORY) == text and builder.calls == []
    note = life.notes.pop()
    assert note.startswith(texts.SURFACE_SWITCH_PREFIX + "desktop")
    assert texts.PLATFORM_HINTS["desktop"] in note
    # The turn carries the note on its user row; a later process reads it back and stays quiet.
    turn = attach_notes({"role": "user", "content": "next"}, note)
    later = [*HISTORY, turn, {"role": "assistant", "content": "ok"}]
    again = PromptLifecycle(SpyBuilder(), store=store)
    assert run(again, desktop, later) == text and not again.notes.pending
    # Back on the prompt's own surface: announced again, pointing at the prompt's own guidance.
    back = PromptLifecycle(SpyBuilder(), store=store)
    assert run(back, make_ctx(scope, tmp_path, platform="cli"), later) == text
    note = back.notes.pop()
    assert note.startswith(texts.SURFACE_SWITCH_PREFIX + "cli")
    assert texts.SURFACE_SWITCH_WHERE_PROMPT in note
    assert texts.PLATFORM_HINTS["cli"] not in note


def test_same_surface_stages_nothing(scope: Scope, tmp_path: Path) -> None:
    store, _ = seeded(scope, tmp_path)
    life = PromptLifecycle(SpyBuilder(), store=store)
    run(life, make_ctx(scope, tmp_path), HISTORY)
    assert not life.notes.pending


def test_note_found_in_multimodal_content() -> None:
    note = texts.SURFACE_SWITCH_NOTE.format(platform="tui", where="x")
    row = {"role": "user", "content": [{"type": "image"}, {"type": "text", "text": note}]}
    assert lc.last_announced_surface([row]) == "tui"
    assert lc.last_announced_surface([{"role": "assistant", "content": note}]) == ""


# -- invalidate and route binding -----------------------------------------------------------------


def test_invalidate_only_for_compaction(scope: Scope, tmp_path: Path) -> None:
    store, text = seeded(scope, tmp_path)
    builder = SpyBuilder()
    life = PromptLifecycle(builder, store=store)
    assert run(life, make_ctx(scope, tmp_path), HISTORY) == text and builder.calls == []
    for reason in ("route_binding", "toolset_change", "memory_reload"):
        with pytest.raises(ValueError, match="only after compaction"):
            life.invalidate(reason)
    life.invalidate("compaction")
    assert life.prompt is None
    run(life, make_ctx(scope, tmp_path), HISTORY)
    assert len(builder.calls) == 1  # rebuilt although the stored prompt still matched
    assert store.saves[-1][1] == life.prompt


def test_rebind_identity_rewrites_only_identity_lines(scope: Scope, tmp_path: Path) -> None:
    store, text = seeded(scope, tmp_path)
    life = PromptLifecycle(SpyBuilder(), store=store)
    run(life, make_ctx(scope, tmp_path), HISTORY)
    updated = asyncio.run(life.rebind_identity(model="model-b", provider="vendor-b"))
    assert updated is not None and updated == life.prompt
    assert updated == text.replace("Model: model-a", "Model: model-b").replace(
        "Provider: vendor-a", "Provider: vendor-b"
    )
    assert store.saves[-1] == ("s1", updated)
    dropped = asyncio.run(life.rebind_identity(model="model-b", provider=""))
    assert dropped is not None and "Provider:" not in dropped
    assert (
        asyncio.run(PromptLifecycle(SpyBuilder()).rebind_identity(model="x", provider="y")) is None
    )


def test_with_identity_without_runtime_block() -> None:
    prompt = "Identity.\n\nConversation date: today\nModel: a\nPlatform: cli"
    out = lc.with_identity(prompt, model="b", provider="p")
    assert out == "Identity.\n\nConversation date: today\nModel: b\nProvider: p\nPlatform: cli"
    assert lc.split_runtime(prompt) == (prompt, "")


# -- the invariant --------------------------------------------------------------------------------


def test_prompt_bytes_unchanged_after_fifty_turns_with_notes(scope: Scope, tmp_path: Path) -> None:
    store, builder = FakeStore(), SpyBuilder()
    life = PromptLifecycle(builder, store=store)
    history: list[dict[str, Any]] = []
    prompts: set[str] = set()
    for turn in range(50):
        prompt = run(life, make_ctx(scope, tmp_path), history)
        prompts.add(prompt)
        life.notes.stage_must_deliver(f"must-deliver note {turn}")
        if turn % 7 == 0:
            life.notes.stage_skills_delta(f"skills changed at turn {turn}")
        user = attach_notes({"role": "user", "content": f"message {turn}"}, life.notes.pop())
        assert f"must-deliver note {turn}" in user["api_content"]
        assert user["content"] == f"message {turn}"
        history += [user, {"role": "assistant", "content": f"reply {turn}"}]
    assert len(prompts) == 1 and len(builder.calls) == 1
    (prompt,) = prompts
    assert "must-deliver note" not in prompt and "skills changed" not in prompt
    # A new process resumes on the same bytes.
    resumed = PromptLifecycle(SpyBuilder(), store=store)
    assert run(resumed, make_ctx(scope, tmp_path), history) == prompt


# -- the P02 store adapter ------------------------------------------------------------------------


@pytest.fixture
def handle(scope: Scope) -> Iterator[registry.StoreHandle]:
    store = registry.open_unshared(scope)
    yield store
    store.close()


def test_session_store_round_trip(
    scope: Scope, tmp_path: Path, handle: registry.StoreHandle
) -> None:
    adapter = SessionPrompts(handle)
    assert asyncio.run(adapter.load("nope")).state == "missing"
    sessions.create(handle, "s1", "cli")
    first = asyncio.run(adapter.load("s1"))
    assert first.state == "null" and first.started_at is not None
    life = PromptLifecycle(SpyBuilder(), store=adapter)
    text = run(life, make_ctx(scope, tmp_path), HISTORY)
    assert asyncio.run(adapter.load("s1")) == StoredPrompt("present", text, first.started_at)
    builder = SpyBuilder()
    assert run(PromptLifecycle(builder, store=adapter), make_ctx(scope, tmp_path), HISTORY) == text
    assert builder.calls == []
    # A missing row is not created by the lifecycle.
    asyncio.run(adapter.save("ghost", "text"))
    assert sessions.get(handle, "ghost") is None


def test_set_system_prompt_replaces_the_stored_text(handle: registry.StoreHandle) -> None:
    sessions.create(handle, "s1", "cli", system_prompt="first")
    assert sessions.set_system_prompt(handle, "s1", "second")
    row = sessions.get(handle, "s1")
    assert row is not None and row["system_prompt"] == "second"
    assert not sessions.set_system_prompt(handle, "absent", "x")
    with pytest.raises(ValueError, match="empty"):
        sessions.set_system_prompt(handle, "s1", "")
