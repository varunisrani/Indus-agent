"""Ratchet: command lookup and known-directory literals live only in ``indus.platform`` (R09.18).

Outside ``src/indus/platform/`` no module may call ``shutil.which(`` or spell a directory from the
known-dir tables. Exceptions go into ``lookup_ratchet_allow.json`` with a reason.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from indus.platform import known_dirs

REPO = Path(__file__).resolve().parents[3]
SRC = REPO / "src"
ALLOW_FILE = Path(__file__).with_name("lookup_ratchet_allow.json")
EXEMPT_PACKAGE = Path("indus") / "platform"
WHICH = "shutil.which("


@dataclass(frozen=True)
class Hit:
    path: str
    line: int
    text: str


def _allowed() -> set[tuple[str, str]]:
    data = json.loads(ALLOW_FILE.read_text(encoding="utf-8"))
    allowed: set[tuple[str, str]] = set()
    for entry in data["entries"]:
        assert entry.get("reason", "").strip(), f"allow-list entry needs a reason: {entry}"
        allowed.add((entry["path"], entry["pattern"]))
    return allowed


def scan(src_root: Path, allowed: set[tuple[str, str]]) -> list[Hit]:
    needles = [WHICH, *sorted(known_dirs.all_literals())]
    hits: list[Hit] = []
    for file in sorted(src_root.rglob("*.py")):
        rel = file.relative_to(src_root)
        if rel.is_relative_to(EXEMPT_PACKAGE):
            continue
        for no, line in enumerate(file.read_text(encoding="utf-8").splitlines(), 1):
            for needle in needles:
                if needle in line and (rel.as_posix(), needle) not in allowed:
                    hits.append(Hit(rel.as_posix(), no, needle))
    return hits


def test_src_is_clean() -> None:
    assert scan(SRC, _allowed()) == []


def test_allow_list_entries_have_reasons() -> None:
    _allowed()


def test_planted_which_is_caught(tmp_path: Path) -> None:
    copy = tmp_path / "src"
    shutil.copytree(SRC, copy)
    planted = copy / "indus" / "foundation" / "planted.py"
    planted.write_text('import shutil\n\nTOOL = shutil.which("git")\n', encoding="utf-8")
    hits = scan(copy, _allowed())
    assert hits == [Hit("indus/foundation/planted.py", 3, WHICH)]


def test_planted_dir_literal_is_caught(tmp_path: Path) -> None:
    copy = tmp_path / "src"
    shutil.copytree(SRC, copy)
    (copy / "indus" / "tools").mkdir(exist_ok=True)
    planted = copy / "indus" / "tools" / "planted.py"
    planted.write_text('CARGO = "~/.cargo/bin"\n', encoding="utf-8")
    assert [h.text for h in scan(copy, _allowed())] == ["~/.cargo/bin"]
    allowed = {("indus/tools/planted.py", "~/.cargo/bin")}
    assert scan(copy, allowed) == []


def test_platform_package_is_exempt(tmp_path: Path) -> None:
    copy = tmp_path / "src"
    shutil.copytree(SRC, copy)
    (copy / "indus" / "platform" / "extra.py").write_text('X = shutil.which("a")\n')
    assert scan(copy, _allowed()) == []
