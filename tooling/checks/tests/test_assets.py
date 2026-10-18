"""Asset hashes."""

from __future__ import annotations

from pathlib import Path

from tooling.checks import assets
from tooling.checks.common import Context


def _bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def test_identical_media_fails_and_different_passes(tmp_path: Path, tiny_cache):
    ref, root = tmp_path / "ref", tmp_path / "repo"
    _bytes(ref / "icons" / "a.png", b"\x89PNG synthetic one")
    _bytes(ref / "fonts" / "f.woff2", b"synthetic font")
    _bytes(ref / "node_modules" / "x" / "b.png", b"\x89PNG vendored")
    _bytes(root / "apps" / "web" / "same.png", b"\x89PNG synthetic one")
    _bytes(root / "apps" / "web" / "own.png", b"\x89PNG ours")
    _bytes(root / "apps" / "web" / "vend.png", b"\x89PNG vendored")
    _bytes(root / "src" / "not_media.txt", b"synthetic font")
    result = assets.run(Context(root=root, ref=ref))
    assert result.code == 1
    assert len(result.violations) == 1
    assert result.violations[0].startswith(
        "apps/web/same.png is identical to reference icons/a.png"
    )


def test_missing_reference(tmp_path: Path):
    assert assets.run(Context(root=tmp_path, ref=None)).code == 0
    assert assets.run(Context(root=tmp_path, ref=None, require_ref=True)).code == 2


def test_cache_reused(tmp_path: Path, tiny_cache: Path):
    ref = tmp_path / "ref"
    _bytes(ref / "a.svg", b"<svg/>")
    first = assets.run(Context(root=tmp_path / "repo", ref=ref))
    second = assets.run(Context(root=tmp_path / "repo", ref=ref))
    assert not any("cache" in n for n in first.notes)
    assert any("cache" in n for n in second.notes)
