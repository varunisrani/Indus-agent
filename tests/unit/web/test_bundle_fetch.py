"""F-3 (wave 46): a script install gets its release's prebuilt dashboard pages, checked from
the release tag.

No network: the git runner and the download are fakes; the archive is built here.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from collections.abc import Sequence
from pathlib import Path

import pytest

from indus.foundation.scope import Scope
from indus.web import bundle as bundle_module
from indus.web.bundle import (
    RELEASE_MANIFEST,
    RELEASE_STAMP,
    BundleLocation,
    FetchError,
    NotOnRelease,
    fetch_release,
    main,
    missing_pages_hint,
    origin_slug,
)
from indus.web.command import Launch, _maybe_build  # pyright: ignore[reportPrivateUsage]

BASE = "https://github.com/indusagi/indus-agent/releases/download/v1.4.0"
ASSET = "indus-web-1.4.0.tar.gz"


def archive(files: dict[str, bytes], *, prefix: str = "") -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(prefix + name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return out.getvalue()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Release:
    """A fake checkout on ``v1.4.0`` plus the release host's two files."""

    def __init__(self, pages: bytes, *, tags: str = "v1.4.0\nv1.3.0\nnot-a-release\n") -> None:
        self.tags = tags
        self.origin = "https://github.com/indusagi/indus-agent.git\n"
        self.pages = pages
        self.manifest = json.dumps({ASSET: sha(pages), "other.zip": "0" * 64}).encode()
        self.record_digest = sha(self.manifest)
        self.downloads: list[str] = []

    def git(self, repo: Path, args: Sequence[str]) -> str:
        if args[:2] == ["tag", "--points-at"]:
            return self.tags
        if args[:2] == ["tag", "--list"]:
            assert args[2] == "v1.4.0"
            record = {"schema": 1, "version": "1.4.0", "manifest_sha256": self.record_digest}
            return json.dumps(record) + "\n-----BEGIN SSH SIGNATURE-----\nxx\n"
        if args[:2] == ["config", "--get"]:
            return self.origin
        raise AssertionError(args)

    def download(self, url: str, cap: int) -> bytes:
        self.downloads.append(url)
        if url == f"{BASE}/{RELEASE_MANIFEST}":
            return self.manifest
        if url == f"{BASE}/{ASSET}":
            return self.pages
        raise AssertionError(url)


def fetch(release: Release, target: Path) -> tuple[str, bool]:
    version, fetched = fetch_release(
        target.parent, target, git=release.git, download=release.download
    )
    return str(version), fetched


def test_the_release_pages_are_checked_and_published_with_a_stamp(tmp_path: Path) -> None:
    release = Release(archive({"index.html": b"<html>1.4</html>", "assets/app.js": b"x"}))
    target = tmp_path / "static"
    assert fetch(release, target) == ("1.4.0", True)
    assert (target / "index.html").read_bytes() == b"<html>1.4</html>"
    assert (target / "assets" / "app.js").is_file()
    assert (target / RELEASE_STAMP).read_text().strip() == "1.4.0"
    assert release.downloads == [f"{BASE}/{RELEASE_MANIFEST}", f"{BASE}/{ASSET}"]
    assert not list(tmp_path.glob(".indus-web-fetch-*")), "no work directory is left"
    assert fetch(release, target) == ("1.4.0", False), "already in place: no download"
    assert len(release.downloads) == 2


def test_an_archive_with_one_top_folder_is_accepted(tmp_path: Path) -> None:
    release = Release(archive({"index.html": b"<html/>"}, prefix="dist/"))
    assert fetch(release, tmp_path / "static")[1]
    assert (tmp_path / "static" / "index.html").is_file()


def test_a_manifest_that_does_not_match_the_tag_record_is_refused(tmp_path: Path) -> None:
    release = Release(archive({"index.html": b"<html/>"}))
    release.record_digest = "1" * 64
    with pytest.raises(FetchError, match="does not match the digest"):
        fetch(release, tmp_path / "static")
    assert release.downloads == [f"{BASE}/{RELEASE_MANIFEST}"]
    assert not (tmp_path / "static").exists()


def test_pages_that_do_not_match_the_manifest_are_refused_and_old_pages_kept(
    tmp_path: Path,
) -> None:
    release = Release(archive({"index.html": b"<html/>"}))
    release.pages = archive({"index.html": b"<html>swapped</html>"})
    target = tmp_path / "static"
    target.mkdir()
    (target / "index.html").write_text("old", encoding="utf-8")
    with pytest.raises(FetchError, match="does not match the release manifest"):
        fetch(release, target)
    assert (target / "index.html").read_text(encoding="utf-8") == "old"


def test_an_archive_that_escapes_its_folder_is_refused(tmp_path: Path) -> None:
    release = Release(archive({"index.html": b"<html/>", "../escape.txt": b"x"}))
    with pytest.raises(FetchError, match="could not be unpacked"):
        fetch(release, tmp_path / "static")
    assert not (tmp_path / "escape.txt").exists()
    assert not (tmp_path / "static").exists()
    assert not list(tmp_path.glob(".indus-web-fetch-*"))


def test_an_archive_without_index_is_refused(tmp_path: Path) -> None:
    release = Release(archive({"app.js": b"x"}))
    with pytest.raises(FetchError, match="no index.html"):
        fetch(release, tmp_path / "static")


def test_a_head_on_no_release_says_so(tmp_path: Path) -> None:
    release = Release(archive({"index.html": b"<html/>"}), tags="v01.2.3\nnightly\n")
    with pytest.raises(NotOnRelease):
        fetch(release, tmp_path / "static")
    assert release.downloads == []


def test_a_tag_without_a_manifest_digest_is_refused(tmp_path: Path) -> None:
    release = Release(archive({"index.html": b"<html/>"}))
    release.record_digest = "not-a-digest"
    with pytest.raises(FetchError, match="records no release manifest digest"):
        fetch(release, tmp_path / "static")
    assert release.downloads == []


def test_a_release_without_the_pages_asset_is_refused(tmp_path: Path) -> None:
    release = Release(archive({"index.html": b"<html/>"}))
    release.manifest = json.dumps({"other.zip": "0" * 64}).encode()
    release.record_digest = sha(release.manifest)
    with pytest.raises(FetchError, match=f"has no {ASSET}"):
        fetch(release, tmp_path / "static")


def test_a_non_github_origin_is_refused(tmp_path: Path) -> None:
    release = Release(archive({"index.html": b"<html/>"}))
    release.origin = "https://git.example.invalid/indus.git"
    with pytest.raises(FetchError, match="not a GitHub repository"):
        fetch(release, tmp_path / "static")
    assert release.downloads == []


@pytest.mark.parametrize(
    ("url", "slug"),
    [
        ("https://github.com/indusagi/indus-agent.git", "indusagi/indus-agent"),
        ("https://github.com/someone/fork", "someone/fork"),
        ("git@github.com:someone/fork.git", "someone/fork"),
        ("ssh://git@github.com/someone/fork.git", "someone/fork"),
        ("https://github.com/a/b/c", None),
        ("http://github.com/a/b", None),
        ("https://github.com.example.invalid/a/b", None),
        ("https://github.com/../b", None),
    ],
)
def test_origin_slug(url: str, slug: str | None) -> None:
    assert origin_slug(url) == slug


def test_the_download_refuses_plain_http() -> None:
    with pytest.raises(FetchError, match="not an HTTPS address"):
        bundle_module._download("http://example.invalid/x", 10)  # pyright: ignore[reportPrivateUsage]


def test_fetch_command_exit_codes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def not_on_release(*_a: object, **_k: object) -> object:
        raise NotOnRelease("this checkout's HEAD is not a release")

    monkeypatch.setattr(bundle_module, "fetch_release", not_on_release)
    assert main(["fetch", "--repo", str(tmp_path)]) == 3
    assert "not a release" in capsys.readouterr().err

    def broken(*_a: object, **_k: object) -> object:
        raise FetchError("download failed")

    monkeypatch.setattr(bundle_module, "fetch_release", broken)
    assert main(["fetch", "--repo", str(tmp_path)]) == 1
    assert "Could not fetch the dashboard pages: download failed" in capsys.readouterr().err


def test_a_missing_build_without_pnpm_says_how_to_get_the_pages(
    scope: Scope,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from indus.web import spa

    monkeypatch.setattr(bundle_module, "locate", lambda: BundleLocation(tmp_path, "missing"))
    monkeypatch.setattr(bundle_module, "source_checkout", lambda: tmp_path)
    monkeypatch.setattr(spa, "build_plan", lambda repo, env: None)
    _maybe_build(Launch(scope=scope, mode="dashboard", host="127.0.0.1", port=0))
    err = capsys.readouterr().err
    assert "The dashboard pages are not built; the API still works." in err
    assert "python -m indus.web.bundle fetch" in err and "pnpm is not on PATH" in err
    assert str(tmp_path) in err
    assert "reinstall from a release" in missing_pages_hint(None)


def test_the_real_git_reader_finds_the_release_record(tmp_path: Path) -> None:
    """The default git runner (explicit minimal env) reads the tag and its record."""
    import shutil
    import subprocess

    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    repo = tmp_path / "checkout"
    repo.mkdir()
    ident = ["-c", "user.name=Test", "-c", "user.email=test@example.invalid"]
    for args in (
        ["init", "-q", "-b", "main"],
        [*ident, "commit", "-q", "--allow-empty", "-m", "one"],
        [*ident, "tag", "--annotate", "v1.4.0", "-m", json.dumps({"manifest_sha256": "a" * 64})],
        ["remote", "add", "origin", "git@github.com:someone/fork.git"],
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True, timeout=60)
    seen: list[str] = []

    def download(url: str, cap: int) -> bytes:
        seen.append(url)
        raise FetchError("offline")

    with pytest.raises(FetchError, match="offline"):
        fetch_release(repo, tmp_path / "static", download=download)
    assert seen == [f"https://github.com/someone/fork/releases/download/v1.4.0/{RELEASE_MANIFEST}"]
