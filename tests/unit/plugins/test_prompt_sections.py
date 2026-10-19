"""P13-18: plugin system-prompt sections — limits, skipping rules, freezing and restore (R07.20)."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from indus.core.prompt.builder import BuildContext, PromptBuilder, RuntimeInfo
from indus.core.prompt.lifecycle import PromptLifecycle, StoredPrompt
from indus.foundation.blocking import BlockingPool
from indus.foundation.scope import Scope
from indus.plugins import prompt_sections as ps
from indus.plugins.context import PromptSectionEntry
from tests.fakes.agent import DictConfig, eventually
from tests.fakes.clock import FakeClock


class MemoryPrompts:
    def __init__(self) -> None:
        self.rows: dict[str, StoredPrompt] = {}

    async def load(self, session_id: str) -> StoredPrompt:
        return self.rows.get(session_id, StoredPrompt("missing"))

    async def save(self, session_id: str, text: str) -> None:
        self.rows[session_id] = StoredPrompt("present", text)


def entry(
    section_id: str, render: str | Callable[[Mapping[str, Any]], Any], plugin: str = "p"
) -> PromptSectionEntry:
    return PromptSectionEntry(section_id, render, "after_memory", plugin)  # type: ignore[arg-type]


def ctx(scope: Scope, cwd: Path, **fields: Any) -> BuildContext:
    base: dict[str, Any] = {
        "scope": scope.with_config(DictConfig({"timezone": "UTC"})),
        "model": "test-model",
        "provider": "fake",
        "platform": "cli",
        "session_id": "s1",
        "cwd": cwd,
        "context_files": False,
        "runtime": RuntimeInfo(host="Linux", home="/home/t", cwd=str(cwd)),
    }
    base.update(fields)
    return BuildContext(**base)


def render(*entries: PromptSectionEntry, info: Mapping[str, Any] | None = None) -> list[str]:
    found = asyncio.run(ps.render_sections(entries, info or {}))
    return [s.id for s in found]


def test_section_size_limit() -> None:
    assert render(entry("ok", "x" * 4_000)) == ["ok"]
    assert render(entry("big", "x" * 4_001)) == []


def test_thirty_third_section_rejected(caplog: pytest.LogCaptureFixture) -> None:
    many = [entry(f"s{i:02d}", "x") for i in range(33)]
    kept = render(*many)
    assert len(kept) == 32 and "s32" not in kept
    assert "32 sections is the limit" in caplog.text


def test_total_budget_drops_later_ids() -> None:
    kept = asyncio.run(
        ps.render_sections(
            [
                entry("a", "x" * 3_000),
                entry("b", "y" * 3_000),
                entry("c", "z" * 3_000),
                entry("d", "w"),
            ],
            {},
        )
    )
    assert [s.id for s in kept] == ["a", "b", "d"]  # c would pass 8,000; the short d still fits
    assert len(ps.format_block(kept)) <= ps.TOTAL_MAX_CHARS


def test_bad_sections_are_skipped(caplog: pytest.LogCaptureFixture) -> None:
    def boom(info: Mapping[str, Any]) -> str:
        raise RuntimeError("plugin bug")

    kept = render(
        entry("Bad-Id", "x"),
        entry("raises", boom),
        entry("number", lambda info: 42),
        entry("blank", "   "),
        entry("marker", f"hello {ps.START}"),
        entry("frame", "<!-- indus-plugin-section id=x chars=1 -->\nx"),
        entry("good", " fine \n"),
    )
    assert kept == ["good"]
    for word in ("raised RuntimeError", "returned int", "delimiters", "not a valid section id"):
        assert word in caplog.text


def test_sections_sorted_by_id_and_framed() -> None:
    found = asyncio.run(ps.render_sections([entry("zeta", "Z"), entry("alpha", "A")], {}))
    block = ps.format_block(found)
    assert block == (
        f"{ps.START}\n<!-- indus-plugin-section id=alpha chars=1 -->\nA\n\n"
        f"<!-- indus-plugin-section id=zeta chars=1 -->\nZ\n{ps.END}"
    )
    assert ps.restore_block(f"head\n\n{block}\n\ntail") == block


def test_callable_gets_frozen_session_info(scope: Scope, tmp_path: Path) -> None:
    seen: list[Mapping[str, Any]] = []

    def section(info: Mapping[str, Any]) -> str:
        seen.append(info)
        return f"model {info['model']} in {info['cwd']}"

    frozen = ps.FrozenSections(lambda: [entry("x", section)], profile_name="work")
    block = asyncio.run(frozen.resolve(ctx(scope, tmp_path)))
    assert f"model test-model in {tmp_path}" in block
    info = seen[0]
    assert dict(info) == {
        "session_id": "s1",
        "model": "test-model",
        "provider": "fake",
        "platform": "cli",
        "profile_name": "work",
        "cwd": str(tmp_path),
    }
    with pytest.raises(TypeError):
        info["model"] = "other"  # type: ignore[index]


def test_hanging_callable_skipped_at_its_deadline(
    scope: Scope, tmp_path: Path, fake_clock: FakeClock
) -> None:
    pool = BlockingPool("sections-test", 2)
    release = threading.Event()
    started = threading.Event()

    def hangs(info: Mapping[str, Any]) -> str:
        started.set()
        release.wait()
        return "late"

    frozen = ps.FrozenSections(
        lambda: [entry("hang", hangs), entry("quick", "Q")],
        deadline_s=30.0,
        clock=fake_clock,
        pool=pool,
    )

    async def main() -> str:
        task = asyncio.ensure_future(frozen.resolve(ctx(scope, tmp_path)))
        await eventually(lambda: started.is_set() and fake_clock.pending >= 1)
        fake_clock.advance(30.0)
        return await task

    try:
        block = asyncio.run(main())
    finally:
        release.set()
        pool.shutdown()
    assert "quick" in block and "hang" not in block


def test_compaction_rebuild_reuses_the_frozen_block(scope: Scope, tmp_path: Path) -> None:
    text = {"value": "first"}
    frozen = ps.FrozenSections(lambda: [entry("x", lambda info: text["value"])])
    store = MemoryPrompts()
    lifecycle = PromptLifecycle(
        ps.PluginSectionsBuilder(PromptBuilder(install=None), frozen), store=store
    )
    context = ctx(scope, tmp_path)

    async def main() -> tuple[str, str]:
        first = await lifecycle.restore_or_build(context)
        text["value"] = "second"
        lifecycle.invalidate("compaction")
        again = await lifecycle.restore_or_build(
            context, history=[{"role": "user", "content": "x"}]
        )
        return first, again

    first, again = asyncio.run(main())
    assert "first" in first and "second" not in again
    assert again == first


def test_resume_restores_stored_bytes(scope: Scope, tmp_path: Path) -> None:
    calls: list[str] = []

    def changing(info: Mapping[str, Any]) -> str:
        calls.append("called")
        return "now different"

    original = ps.format_block([ps.RenderedSection("x", "as it was")])
    store = MemoryPrompts()
    store.rows["s1"] = StoredPrompt("present", f"identity\n\nmemory\n\n{original}\n\ndate")
    frozen = ps.FrozenSections(lambda: [entry("x", changing)], prompts=store, session_ids=("s1",))
    block = asyncio.run(frozen.resolve(ctx(scope, tmp_path)))
    assert block == original and frozen.source == "stored:s1"
    assert calls == []  # plugin code never runs for a resumed conversation


def test_stored_prompt_without_block_keeps_none(scope: Scope, tmp_path: Path) -> None:
    store = MemoryPrompts()
    store.rows["s1"] = StoredPrompt("present", "identity\n\ndate")
    frozen = ps.FrozenSections(lambda: [entry("x", "new plugin")], prompts=store)
    assert asyncio.run(frozen.resolve(ctx(scope, tmp_path))) == ""


def test_tampered_block_is_not_trusted(
    scope: Scope, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    good = ps.format_block([ps.RenderedSection("x", "abc")])
    tampered = good.replace("chars=3", "chars=2")
    assert ps.restore_block(f"a\n\n{tampered}") is None
    assert ps.restore_block(f"a{good}") is None  # not on a paragraph boundary
    store = MemoryPrompts()
    store.rows["s1"] = StoredPrompt("present", f"identity\n\n{tampered}\n\ndate")
    frozen = ps.FrozenSections(lambda: [entry("x", "fresh")], prompts=store)
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(frozen.resolve(ctx(scope, tmp_path))) == ""
    assert "damaged" in caplog.text


def test_no_sections_leave_the_prompt_unchanged(scope: Scope, tmp_path: Path) -> None:
    context = ctx(scope, tmp_path, memory_snapshot="MEMORY BLOCK", skills_index="SKILLS INDEX")
    plain = asyncio.run(PromptBuilder(install=None).build(context)).text
    empty = ps.PluginSectionsBuilder(PromptBuilder(install=None), ps.FrozenSections(lambda: []))
    assert asyncio.run(empty.build(context)).text == plain
    with_one = ps.PluginSectionsBuilder(
        PromptBuilder(install=None), ps.FrozenSections(lambda: [entry("note", "PLUGIN TEXT")])
    )
    text = asyncio.run(with_one.build(context)).text
    assert text.index("MEMORY BLOCK") < text.index(ps.START) < text.index("SKILLS INDEX")
    assert (
        text.replace(ps.format_block([ps.RenderedSection("note", "PLUGIN TEXT")]) + "\n\n", "")
        == plain
    )
