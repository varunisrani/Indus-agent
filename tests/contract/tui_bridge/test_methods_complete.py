"""Completion and composer helpers (P12-31): remote listings (R05.13), fuzzy search, hints, slash
completion, paste files, dropped paths and staged images."""

from __future__ import annotations

import asyncio
import re
import stat
from pathlib import Path

from indus.foundation.scope import Scope
from indus.protocol.errors import INVALID_PARAMS
from tests.contract.tui_bridge._harness import open_rig
from tests.fakes.agent import DictConfig
from tests.fakes.backend.remote_fs import BACKEND, RemoteTree, registry_with
from tests.fakes.clock import FakeClock
from tests.fakes.model import text_turn

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def texts(result: dict[str, object]) -> list[str]:
    items = result["items"]
    assert isinstance(items, list)
    return [str(item["text"]) for item in items]  # pyright: ignore[reportUnknownArgumentType]


def test_remote_backend_lists_its_own_tree(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    host = tmp_path / "proj"
    (host / "host_only").mkdir(parents=True)
    tree = RemoteTree(
        {
            "home": {
                "remote": {
                    "proj": {
                        "src": {"main.py": None, "util.py": None},
                        "docs": {},
                        "README.md": None,
                        ".env": None,
                    }
                }
            }
        }
    )
    remote_scope = scope.with_config(DictConfig({"terminal": {"backend": BACKEND}}))

    async def main() -> None:
        environments = registry_with(tree)
        async with open_rig(remote_scope, fake_clock, tmp_path, environments=environments) as rig:
            cwd = "/home/remote/proj"
            folders = await rig.ok("complete.path", word="@folder:", cwd=cwd)
            assert texts(folders) == ["@folder:docs/", "@folder:src/"]
            files = await rig.ok("complete.path", word="src/m", cwd=cwd)
            assert texts(files) == ["src/main.py"]
            bare = await rig.ok("complete.path", word="@ma", cwd=cwd)
            assert texts(bare) == []  # no fuzzy walk on a remote backend; nothing starts with "ma"
            plain = await rig.ok("complete.path", word="RE", cwd=cwd)
            assert texts(plain) == ["README.md"]
            home = await rig.ok("complete.path", word="~/pr", cwd=cwd)
            assert texts(home) == ["~/proj/"]
            assert all(
                "host_only" not in t
                for t in texts(await rig.ok("complete.path", word="./", cwd=cwd))
            )
            assert tree.commands and all(c.startswith("sh -c ") for c in tree.commands)
            await environments.shutdown()

    asyncio.run(main())


def test_bare_word_fuzzy_search_is_capped(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    root = tmp_path / "work"
    for n in range(40):
        (root / "deep").mkdir(parents=True, exist_ok=True)
        (root / "deep" / f"match_{n:02d}.txt").write_text("x", encoding="utf-8")
    (root / "match_dir").mkdir()
    (root / ".git").mkdir()
    (root / ".git" / "match_hidden").write_text("x", encoding="utf-8")

    async def main() -> None:
        async with open_rig(scope, fake_clock, tmp_path) as rig:
            found = texts(await rig.ok("complete.path", word="@match", cwd=str(root)))
            assert len(found) == 30
            assert found[0] == "@folder:match_dir/"
            assert all("hidden" not in t for t in found)
            assert found[1] == "@file:deep/match_00.txt"
            one = texts(await rig.ok("complete.path", word="@file:mat", cwd=str(root)))
            assert "@folder:match_dir/" not in one
            listing = texts(await rig.ok("complete.path", word="@folder:", cwd=str(root)))
            assert listing == ["@folder:deep/", "@folder:match_dir/"]

    asyncio.run(main())


def test_reference_hints_and_other_kinds(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        async with open_rig(scope, fake_clock, tmp_path) as rig:
            hints = texts(await rig.ok("complete.path", word="@", cwd=str(tmp_path)))
            assert hints[:2] == ["@file:", "@folder:"] and "@diff" in hints
            assert texts(await rig.ok("complete.path", word="@url:http", cwd=str(tmp_path))) == []
            assert texts(await rig.ok("complete.path", word="")) == []

    asyncio.run(main())


def test_slash_completion_uses_the_registry_and_quick_commands(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    (scope.home / "config.yaml").write_text(
        "quick_commands:\n  gs:\n    type: exec\n    command: git status\n", encoding="utf-8"
    )

    async def main() -> None:
        async with open_rig(scope, fake_clock, tmp_path) as rig:
            found = await rig.ok("complete.slash", text="/he")
            assert "/help" in texts(found)
            quick = await rig.ok("complete.slash", text="/g")
            assert "/gs" in texts(quick) and "/goal" in texts(quick)
            subs = await rig.ok("complete.slash", text="/reasoning hig")
            assert texts(subs) == ["high"]

    asyncio.run(main())


def test_paste_collapse_writes_a_private_file(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        async with open_rig(scope, fake_clock, tmp_path) as rig:
            text = "\n".join(f"row {n}" for n in range(12))
            first = await rig.ok("paste.collapse", text=text)
            second = await rig.ok("paste.collapse", text="one")
            path = Path(first["path"])
            assert path.parent == scope.home / "pastes"
            assert re.fullmatch(r"paste_1_\d{6}\.txt", path.name)
            assert Path(second["path"]).name.startswith("paste_2_")
            assert path.read_text(encoding="utf-8") == text
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            assert first["lines"] == 12 and "#1" in first["placeholder"]
            empty = await rig.err("paste.collapse", text="  ")
            assert empty["data"]["path"] == "text"

    asyncio.run(main())


def test_dropped_paths_are_detected(scope: Scope, fake_clock: FakeClock, tmp_path: Path) -> None:
    spaced = tmp_path / "my shot.png"
    spaced.write_bytes(PNG)
    notes = tmp_path / "notes.txt"
    notes.write_text("n", encoding="utf-8")

    async def main() -> None:
        async with open_rig(scope, fake_clock, tmp_path) as rig:
            quoted = await rig.ok("input.detect_drop", text=f"'{spaced}' {notes}")
            assert quoted["is_drop"] is True
            assert quoted["paths"] == [str(spaced), str(notes)] and quoted["images"] == [
                str(spaced)
            ]
            escaped = str(spaced).replace(" ", "\\ ")
            assert (await rig.ok("input.detect_drop", text=escaped))["paths"] == [str(spaced)]
            url = f"file://{str(notes).replace(' ', '%20')}"
            assert (await rig.ok("input.detect_drop", text=url))["is_drop"] is True
            prose = await rig.ok("input.detect_drop", text="please fix the tests")
            assert prose == {"is_drop": False, "paths": [], "images": []}

    asyncio.run(main())


def test_attached_images_ride_on_the_next_prompt(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    image = tmp_path / "chart.png"
    image.write_bytes(PNG)
    text_file = tmp_path / "chart.txt"
    text_file.write_text("not an image", encoding="utf-8")

    async def main() -> None:
        async with open_rig(scope, fake_clock, tmp_path, text_turn("I see it")) as rig:
            sid = (await rig.ok("session.create", cwd=str(tmp_path)))["session_id"]
            staged = await rig.ok("image.attach", session_id=sid, path="chart.png")
            assert staged == {"name": "chart.png", "path": str(image), "attached": 1}
            refused = await rig.err("image.attach", session_id=sid, path=str(text_file))
            assert refused["code"] == INVALID_PARAMS.code and refused["data"]["path"] == "path"
            await rig.ok("prompt.submit", session_id=sid, text="what is this?")
            await rig.turn_done()
            await rig.idle(sid)
            history = await rig.ok("session.history", session_id=sid)
            first = history["messages"][0]["text"]
            assert first.startswith("what is this?") and f"[Attached image: {image}]" in first
            view = rig.bridge.view(sid)
            assert view is not None and view.images == []

    asyncio.run(main())
