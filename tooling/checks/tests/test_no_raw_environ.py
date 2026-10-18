"""The ``no_raw_environ`` guard (P05-05)."""

from __future__ import annotations

import json
from pathlib import Path

from tooling.checks import NAMES, no_raw_environ
from tooling.checks.common import DEFAULT_ROOT
from tooling.checks.tests.conftest import make_ctx, write


def allow_file(tmp_path: Path, allow: dict[str, str]) -> Path:
    path = tmp_path / "allow.json"
    path.write_text(json.dumps({"note": "test", "allow": allow}), encoding="utf-8")
    return path


def test_registered() -> None:
    assert "no_raw_environ" in NAMES


def test_flags_seeded_dict_of_environ(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    write(
        root / "src" / "pkg" / "spawn.py",
        "import os\nimport subprocess\n\n"
        "def go():\n    env = dict(os.environ)\n    subprocess.run(['ls'], env=env)\n",
    )
    result = no_raw_environ.run(make_ctx(root), allow_file(tmp_path, {}))
    assert result.code == 1
    assert any("src/pkg/spawn.py:5: os.environ" in v for v in result.violations)


def test_every_spelling_is_caught() -> None:
    source = (
        "import os as o\n"
        "from os import getenv, path\n"
        "a = o.getenv('X')\n"
        "o.putenv('X', '1')\n"
        "b = o.environb\n"
    )
    hits = [h.what for h in no_raw_environ.find_hits(source)]
    assert hits == ["from os import getenv", "o.getenv", "o.putenv", "o.environb"]


def test_lookalikes_are_not_flagged() -> None:
    source = (
        "import os\n"
        "env = scope.env\n"
        "x = os.path.join('a', 'b')\n"
        "text = 'os.environ is mentioned in a string'\n"
        "obj.environ = {}\n"
    )
    assert no_raw_environ.find_hits(source) == []


def test_allow_list_needs_reason_and_reports_stale(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    write(root / "src" / "boot.py", "import os\nHOME = os.environ.get('HOME')\n")
    write(root / "src" / "clean.py", "x = 1\n")
    allow = allow_file(
        tmp_path,
        {"src/boot.py": "bootstrap", "src/clean.py": "old reason", "src/gone.py": "removed"},
    )
    result = no_raw_environ.run(make_ctx(root), allow)
    assert result.code == 0
    assert len(result.warnings) == 2
    bad = allow_file(tmp_path, {"src/boot.py": " "})
    result = no_raw_environ.run(make_ctx(root), bad)
    assert result.code == 1 and any("needs a non-empty reason" in v for v in result.violations)


def test_unparsable_file_is_a_violation(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    write(root / "src" / "broken.py", "def (:\n")
    result = no_raw_environ.run(make_ctx(root), allow_file(tmp_path, {}))
    assert result.code == 1 and "cannot parse" in result.violations[0]


def test_root_without_src_is_a_note(tmp_path: Path) -> None:
    result = no_raw_environ.run(make_ctx(tmp_path), allow_file(tmp_path, {}))
    assert result.code == 0 and result.notes


def test_this_repository_is_clean() -> None:
    result = no_raw_environ.run(make_ctx(DEFAULT_ROOT))
    assert result.code == 0, result.violations
    assert not result.warnings, result.warnings
