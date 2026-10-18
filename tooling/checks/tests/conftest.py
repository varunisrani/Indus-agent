"""Shared helpers for the guard-check tests."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tooling.checks.common import Context, resolve_ref

FIXTURES = Path(__file__).parent / "fixtures"

# The fixture trees contain sample test modules; they are data, not tests to collect.
collect_ignore_glob = ["fixtures/*"]


@pytest.fixture
def copy_fixture(tmp_path: Path):
    def _copy(name: str) -> Path:
        dest = tmp_path / name
        shutil.copytree(FIXTURES / name, dest)
        return dest

    return _copy


@pytest.fixture
def real_ref() -> Path:
    ref = resolve_ref(None)
    if ref is None or not ref.is_dir():
        pytest.skip("reference checkout not available")
    return ref


@pytest.fixture
def tiny_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cache = tmp_path / "cache"
    monkeypatch.setenv("INDUS_CHECKS_CACHE", str(cache))
    monkeypatch.setenv("INDUS_CHECKS_JOBS", "1")
    return cache


def make_ctx(root: Path, **kw) -> Context:
    return Context(root=root, **kw)


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path
