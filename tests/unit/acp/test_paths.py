"""P19-05/07: editor folders — WSL translation and comparison (R09.16)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from indus.acp import paths


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("E:\\Projects\\x", "/mnt/e/Projects/x"),
        ("e:/Projects/x/", "/mnt/e/Projects/x"),
        ("C:\\", "/mnt/c"),
        ("D:", "/mnt/d"),
        ("/home/me", None),
        ("relative\\path", None),
    ],
)
def test_drive_paths_become_mounts(given: str, expected: str | None) -> None:
    assert paths.drive_to_mount(given) == expected


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("\\\\wsl.localhost\\Ubuntu\\home\\me\\proj", "/home/me/proj"),
        ("\\\\wsl$\\Debian\\srv", "/srv"),
        ("//wsl.localhost/Ubuntu/home/me", "/home/me"),
        ("\\\\WSL.LOCALHOST\\Ubuntu", "/"),
        ("\\\\server\\share\\x", None),
    ],
)
def test_wsl_unc_paths_become_posix(given: str, expected: str | None) -> None:
    assert paths.unc_to_posix(given) == expected


def test_translation_happens_only_under_wsl() -> None:
    assert paths.translate_cwd("E:\\Projects\\x", wsl=True) == "/mnt/e/Projects/x"
    assert paths.translate_cwd("\\\\wsl.localhost\\U\\home\\a", wsl=True) == "/home/a"
    assert paths.translate_cwd("/home/a", wsl=True) == "/home/a"
    assert paths.translate_cwd("E:\\Projects\\x", wsl=False) == "E:\\Projects\\x"
    assert paths.translate_cwd("E:\\x", wsl=lambda: True) == "/mnt/e/x"


def test_symlinked_spellings_compare_equal(tmp_path: Path) -> None:
    # macOS: /var -> /private/var and /tmp -> /private/tmp (R09.16); a symlink models it here.
    real = tmp_path / "private" / "var"
    (real / "x").mkdir(parents=True)
    alias = tmp_path / "var"
    os.symlink(real, alias)
    assert paths.same_folder(str(alias / "x"), str(real / "x"))
    assert paths.same_folder(str(alias / "x") + "/", str(real / "x"))
    assert not paths.same_folder(str(alias / "x"), str(real))


def test_drive_and_mount_spellings_compare_equal() -> None:
    assert paths.comparable("E:\\Work\\a") == paths.comparable("/mnt/e/Work/a")
    assert paths.comparable("/mnt/E/Work/a") == paths.comparable("/mnt/e/Work/a")
    assert paths.comparable("~/x") == paths.comparable(os.path.expanduser("~/x"))


def test_missing_folders_compare_by_spelling(tmp_path: Path) -> None:
    gone = tmp_path / "no" / "such"
    assert paths.same_folder(str(gone), str(gone) + "/./")
