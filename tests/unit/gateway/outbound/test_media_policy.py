"""P15-18: media tags and the outbound path policy (security boundary, R06.22)."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from indus.foundation.scope import Scope, make_scope
from indus.gateway.outbound.media_policy import (
    MediaPolicy,
    MediaRef,
    extract_media,
    fetch_remote,
    screen_remote_path,
    strip_media_tags,
)
from tests.fakes.clock import FakeClock


@pytest.fixture
def fake_home(tmp_path: Path) -> Path:
    home = tmp_path / "userhome"
    home.mkdir()
    return home


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime.now(UTC).replace(microsecond=0))


@pytest.fixture
def media_scope(tmp_path: Path, fake_home: Path, clock: FakeClock) -> Scope:
    profile = tmp_path / "indus-root" / "profiles" / "work"
    profile.mkdir(parents=True)
    return make_scope(profile, env={"HOME": str(fake_home)}, clock=clock)


def _file(path: Path, *, age: float, clock: FakeClock, data: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    stamp = clock.now().timestamp() - age
    os.utime(path, (stamp, stamp))
    return path


def _cache(scope: Scope, name: str) -> Path:
    return scope.home / "cache" / "documents" / name


# -- extraction -----------------------------------------------------------------------------------


def test_tags_are_extracted_and_removed() -> None:
    text = "Here is the chart:\nMEDIA:/tmp/out/chart.png\nand **MEDIA:/tmp/r.pdf**. Done"
    clean, refs = extract_media(text)
    assert [r.raw for r in refs] == ["/tmp/out/chart.png", "/tmp/r.pdf"]
    assert [r.kind for r in refs] == ["image", "document"]
    assert "MEDIA" not in clean
    assert clean == "Here is the chart:\n\nand  Done"


def test_tag_inside_code_is_ignored() -> None:
    text = "Example:\n```\nMEDIA:/etc/passwd\n```\nand `MEDIA:/etc/shadow` inline"
    clean, refs = extract_media(text)
    assert refs == []
    assert clean == text
    unclosed = "```\nMEDIA:/home/u/.ssh/id_rsa"
    assert extract_media(unclosed) == (unclosed, [])


def test_glued_quoted_and_flagged_tags() -> None:
    clean, refs = extract_media(
        'MEDIA:/a/x.pngMEDIA:/a/y.png MEDIA:"/a/my file.ogg" [[audio_as_voice]]'
    )
    assert [r.raw for r in refs] == ["/a/x.png", "/a/y.png", "/a/my file.ogg"]
    assert refs[2].kind == "voice"
    assert clean == ""
    _, docs = extract_media("MEDIA:/a/x.png [[as_document]]")
    assert docs[0].kind == "document"


def test_image_markdown_and_prose_mentions() -> None:
    clean, refs = extract_media(
        "![plot](https://e.com/p.png) and ![local](/tmp/p.png) ![rel](p.png)"
    )
    assert [(r.raw, r.is_url) for r in refs] == [
        ("https://e.com/p.png", True),
        ("/tmp/p.png", False),
    ]
    assert clean == "and  ![rel](p.png)"
    prose = "Social media: /r/python is fun"
    assert extract_media(prose) == (prose, [])
    assert strip_media_tags("look MEDIA:/x/y.png") == "look"


# -- the local policy -----------------------------------------------------------------------------


def test_cache_file_is_allowed(media_scope: Scope, clock: FakeClock) -> None:
    path = _file(_cache(media_scope, "report.pdf"), age=86_400 * 30, clock=clock)
    verdict = MediaPolicy.for_scope(media_scope).check(str(path))
    assert verdict.path == os.path.realpath(path)


def test_symlink_from_cache_to_ssh_key_is_refused(
    media_scope: Scope, fake_home: Path, clock: FakeClock
) -> None:
    key = _file(fake_home / ".ssh" / "id_ed25519", age=10, clock=clock)
    link = _cache(media_scope, "innocent.png")
    link.parent.mkdir(parents=True)
    link.symlink_to(key)
    verdict = MediaPolicy.for_scope(media_scope).check(str(link))
    assert verdict.path is None
    assert verdict.reason == "not allowed"


@pytest.mark.parametrize(
    "where",
    [
        "home:.ssh/id_rsa",
        "home:.aws/credentials",
        "home:.gnupg/private.key",
        "home:.netrc",
        "home:.config/gcloud/creds.json",
        "profile:secrets.json",
        "profile:.env",
        "profile:auth.json",
        "profile:config.yaml",
        "profile:state.db",
        "profile:sessions/one.jsonl",
        "profile:pairing/telegram.json",
        "tmp:project/.env",
        "tmp:project/.env.production",
    ],
)
def test_deny_list_table(
    where: str, media_scope: Scope, fake_home: Path, tmp_path: Path, clock: FakeClock
) -> None:
    base, rel = where.split(":")
    root = {"home": fake_home, "profile": media_scope.home, "tmp": tmp_path}[base]
    path = _file(root / rel, age=5, clock=clock)  # recent: trust by age must not override
    trusting = MediaPolicy.for_scope(media_scope, {"media_trust_recent": True})
    assert trusting.check(str(path)).reason == "not allowed"


@pytest.mark.parametrize("path", ["/etc/passwd", "/proc/self/environ", "/dev/zero", "/sys/kernel"])
def test_system_paths_are_refused(path: str, media_scope: Scope) -> None:
    assert not MediaPolicy.for_scope(media_scope).check(path).ok


def test_recent_file_trust_window(media_scope: Scope, tmp_path: Path, clock: FakeClock) -> None:
    policy = MediaPolicy.for_scope(media_scope, {"media_trust_recent": True})
    fresh = _file(tmp_path / "out" / "fresh.pdf", age=599, clock=clock)
    stale = _file(tmp_path / "out" / "stale.pdf", age=601, clock=clock)
    future = _file(tmp_path / "out" / "future.pdf", age=-3600, clock=clock)
    assert policy.check(str(fresh)).ok
    assert not policy.check(str(stale)).ok
    assert not policy.check(str(future)).ok
    clock.advance(2)
    assert not policy.check(str(fresh)).ok
    off = MediaPolicy.for_scope(media_scope)  # the default (lead decision): recency trusts nothing
    assert not off.trust_recent
    assert not off.check(str(_file(tmp_path / "out" / "now.pdf", age=1, clock=clock))).ok


def test_policy_reads_the_profiles_gateway_section(
    media_scope: Scope, tmp_path: Path, clock: FakeClock
) -> None:
    exports = tmp_path / "exports"
    (media_scope.home / "config.yaml").write_text(
        "gateway:\n"
        "  media_trust_recent: true\n"
        "  media_trust_recent_seconds: 30\n"
        f"  media_allow_dirs: [{str(exports)!r}]\n",
        encoding="utf-8",
    )
    policy = MediaPolicy.for_scope(media_scope)
    assert policy.trust_recent and policy.recent_seconds == 30.0
    assert policy.extra_dirs == (exports,)
    assert policy.check(str(_file(tmp_path / "out" / "a.pdf", age=20, clock=clock))).ok
    assert not policy.check(str(_file(tmp_path / "out" / "b.pdf", age=40, clock=clock))).ok


def test_strict_mode_refuses_recent_but_accepts_allowed_dir(
    media_scope: Scope, tmp_path: Path, clock: FakeClock
) -> None:
    allowed = tmp_path / "exports"
    strict = MediaPolicy.for_scope(
        media_scope, {"media_strict": True, "media_allow_dirs": [str(allowed)]}
    )
    recent = _file(tmp_path / "out" / "fresh.pdf", age=5, clock=clock)
    assert strict.check(str(recent)).reason == "not in a folder media may be sent from"
    listed = _file(allowed / "old.csv", age=86_400 * 365, clock=clock)
    assert strict.check(str(listed)).ok


def test_an_old_file_named_by_model_or_tool_text_is_never_sent(
    media_scope: Scope, fake_home: Path, clock: FakeClock
) -> None:
    diary = _file(fake_home / "notes" / "diary.txt", age=86_400 * 90, clock=clock)
    tool_output = f"Search result: see MEDIA:{diary} for details"
    _, refs = extract_media(tool_output)
    items, notices = asyncio.run(MediaPolicy.for_scope(media_scope).resolve(refs))
    assert items == []
    assert notices == ["Could not attach diary.txt: not in a folder media may be sent from."]


def test_unusable_paths(media_scope: Scope, tmp_path: Path) -> None:
    policy = MediaPolicy.for_scope(media_scope)
    assert policy.check("relative/file.png").reason == "not an absolute path"
    assert policy.check("~other/file.png").reason == "not an absolute path"
    assert policy.check("/no/such/file.png").not_found
    assert policy.check(str(tmp_path)).reason == "not a regular file"
    assert policy.check("\\\\?\\C:\\x").reason == "not a usable path"


def test_home_relative_path_uses_the_scope_home(media_scope: Scope, clock: FakeClock) -> None:
    path = _file(_cache(media_scope, "a.pdf"), age=1, clock=clock)
    policy = MediaPolicy.for_scope(media_scope, {"media_allow_dirs": ["~/exports"]})
    assert policy.check(str(path)).ok
    home = media_scope.env["HOME"]
    assert policy.extra_dirs == (Path(home) / "exports",)


def test_resolve_mixes_urls_files_and_notices(media_scope: Scope, clock: FakeClock) -> None:
    good = _file(_cache(media_scope, "a.png"), age=1, clock=clock)
    refs = [
        MediaRef("https://e.com/x.jpg", is_url=True),
        MediaRef(str(good)),
        MediaRef("/nope.pdf"),
    ]
    items, notices = asyncio.run(MediaPolicy.for_scope(media_scope).resolve(refs))
    assert [(i.kind, i.name) for i in items] == [("image", "x.jpg"), ("image", "a.png")]
    assert notices == ["Could not attach nope.pdf: file not found."]


# -- remote sandboxes -----------------------------------------------------------------------------


class FakeRemote:
    def __init__(self, links: dict[str, str], home: str | None = "/home/agent") -> None:
        self.links = links
        self._home = home
        self.fetched: list[str] = []
        self.resolved: list[str] = []

    @property
    def home(self) -> str | None:
        return self._home

    async def realpath(self, path: str) -> str | None:
        self.resolved.append(path)
        return self.links.get(path, path)

    async def fetch(self, path: str, dest: Path, max_bytes: int) -> None:
        self.fetched.append(path)
        dest.write_bytes(b"remote bytes")


def test_remote_path_is_screened_twice(media_scope: Scope) -> None:
    remote = FakeRemote({"/work/report.pdf": "/home/agent/.ssh/id_rsa"})
    policy = MediaPolicy.for_scope(media_scope)
    verdict = asyncio.run(fetch_remote(policy, remote, "/work/report.pdf"))
    assert verdict.reason == "not allowed"
    assert remote.resolved == ["/work/report.pdf"]
    assert remote.fetched == []
    blocked = asyncio.run(fetch_remote(policy, remote, "~/.aws/credentials"))
    assert not blocked.ok and remote.resolved == ["/work/report.pdf"]


def test_remote_file_is_fetched_into_the_document_cache(media_scope: Scope) -> None:
    remote = FakeRemote({})
    policy = MediaPolicy.for_scope(media_scope)
    verdict = asyncio.run(fetch_remote(policy, remote, "/work/out.pdf"))
    assert verdict.path is not None
    assert Path(verdict.path).parent == policy.document_cache.resolve()
    assert remote.fetched == ["/work/out.pdf"]
    items, _ = asyncio.run(policy.resolve([MediaRef("/work/other.pdf")], remote=remote))
    assert len(items) == 1 and remote.fetched[-1] == "/work/other.pdf"


def test_strict_mode_never_fetches(media_scope: Scope) -> None:
    remote = FakeRemote({})
    strict = MediaPolicy.for_scope(media_scope, {"media_strict": True})
    assert not asyncio.run(fetch_remote(strict, remote, "/work/out.pdf")).ok
    assert remote.fetched == [] and remote.resolved == []


def test_remote_screen_without_a_known_home() -> None:
    assert not screen_remote_path("/srv/u/.ssh/key", None)
    assert not screen_remote_path("/x/.indus/secrets.json", None)
    assert not screen_remote_path("/etc/shadow", None)
    assert not screen_remote_path("relative", None)
    assert screen_remote_path("/work/out.pdf", None)
    assert screen_remote_path("/srv/u/.sshx/key", None)


def test_more_credential_stores_are_refused(
    media_scope: Scope, fake_home: Path, clock: FakeClock
) -> None:
    policy = MediaPolicy.for_scope(media_scope, {"media_trust_recent": True})
    root = media_scope.root
    for path in (
        fake_home / ".config" / "gh" / "hosts.yml",
        root / "profiles" / "other" / "state.db",
        root / "profiles" / "other" / "cache" / "documents" / ".env",
        media_scope.home / "cache" / "browser" / "profiles" / "p1" / "Cookies",
        media_scope.home / "backups" / "backup.zip",
    ):
        assert policy.check(str(_file(path, age=5, clock=clock))).reason == "not allowed", path


def test_a_profile_under_a_denied_prefix_still_sends_its_cache(
    tmp_path: Path, fake_home: Path, clock: FakeClock
) -> None:
    profile = fake_home / ".config" / "indus"
    profile.mkdir(parents=True)
    scope = make_scope(profile, env={"HOME": str(fake_home)}, clock=clock)
    cached = _file(profile / "cache" / "images" / "a.png", age=99_999, clock=clock)
    secret = _file(profile / "secrets.json", age=5, clock=clock)
    policy = MediaPolicy.for_scope(scope)
    assert policy.check(str(cached)).ok
    assert not policy.check(str(secret)).ok
