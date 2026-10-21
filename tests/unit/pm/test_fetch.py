"""P24-23: verified fetch with mirror fallback, resumable ranges, timeouts, https-only."""

from __future__ import annotations

from pathlib import Path

import pytest

from indus.pm.fetch import Fetcher, FetchFailed, mirror_url
from indus.pm.lock import PmError
from tests.fakes.pm_origin import MIRROR, FakeOrigin, sha256

DATA = bytes(range(256)) * 40  # 10,240 bytes
DIGEST = sha256(DATA)


def make(tmp_path: Path, origin: FakeOrigin, **kw: object) -> Fetcher:
    settings: dict[str, object] = {
        "mirror": MIRROR,
        "timeout": 7.5,
        "transport": origin.transport(),
    }
    settings.update(kw)
    return Fetcher(tmp_path / "downloads", **settings)  # type: ignore[arg-type]


def fetch(tmp_path: Path, origin: FakeOrigin, url: str, digest: str = DIGEST, **kw: object) -> Path:
    fetcher = make(tmp_path, origin, **kw)
    try:
        return fetcher.fetch(url, digest)
    finally:
        fetcher.close()


def test_mirror_url_is_content_addressed() -> None:
    assert mirror_url("https://m.example/x/", "a" * 64) == "https://m.example/x/sha256/" + "a" * 64
    with pytest.raises(PmError):
        mirror_url("https://m.example", "abc")


def test_a_hash_mismatch_at_the_primary_falls_back_to_the_mirror(
    tmp_path: Path, origin: FakeOrigin
) -> None:
    url = origin.add("a.bin", DATA)
    origin.wrong.add(url)
    blob = fetch(tmp_path, origin, url)
    assert blob == tmp_path / "downloads" / DIGEST
    assert blob.read_bytes() == DATA
    assert origin.urls() == [url, f"{MIRROR}/sha256/{DIGEST}"]
    assert not list((tmp_path / "downloads").glob("*.part"))


def test_the_mirror_must_verify_too(tmp_path: Path, origin: FakeOrigin) -> None:
    url = origin.add("a.bin", DATA)
    origin.wrong.update({url, f"{MIRROR}/sha256/{DIGEST}"})
    with pytest.raises(FetchFailed) as caught:
        fetch(tmp_path, origin, url)
    text = str(caught.value)
    assert f"primary {url}: sha256 mismatch" in text
    assert "mirror https://mirror.example/pm/sha256/" in text
    assert not (tmp_path / "downloads" / DIGEST).exists()


def test_without_a_mirror_a_bad_primary_fails(tmp_path: Path, origin: FakeOrigin) -> None:
    url = origin.add("a.bin", DATA)
    origin.wrong.add(url)
    with pytest.raises(FetchFailed, match="pm.mirror_url"):
        fetch(tmp_path, origin, url, mirror="")
    assert origin.urls() == [url]


def test_a_missing_primary_falls_back_to_the_mirror(tmp_path: Path, origin: FakeOrigin) -> None:
    url = origin.add("a.bin", DATA)
    origin.status[url] = 404
    assert fetch(tmp_path, origin, url).read_bytes() == DATA


def test_a_dropped_connection_resumes_with_a_range_request(
    tmp_path: Path, origin: FakeOrigin
) -> None:
    url = origin.add("a.bin", DATA)
    origin.cut_once[url] = 4000
    assert fetch(tmp_path, origin, url).read_bytes() == DATA
    assert [s.headers.get("range") for s in origin.seen] == [None, "bytes=4000-"]


def test_a_part_left_by_an_earlier_run_is_continued(tmp_path: Path, origin: FakeOrigin) -> None:
    url = origin.add("a.bin", DATA)
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    (downloads / f"{DIGEST}.part").write_bytes(DATA[:1000])
    assert fetch(tmp_path, origin, url).read_bytes() == DATA
    assert [s.headers.get("range") for s in origin.seen] == ["bytes=1000-"]


def test_a_server_that_ignores_ranges_restarts_from_zero(
    tmp_path: Path, origin: FakeOrigin
) -> None:
    url = origin.add("a.bin", DATA)
    origin.ignore_range.add(url)
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    (downloads / f"{DIGEST}.part").write_bytes(DATA[:1000])
    assert fetch(tmp_path, origin, url).read_bytes() == DATA


def test_a_complete_part_answered_416_is_checked_and_kept(
    tmp_path: Path, origin: FakeOrigin
) -> None:
    url = origin.add("a.bin", DATA)
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    (downloads / f"{DIGEST}.part").write_bytes(DATA)
    assert fetch(tmp_path, origin, url).read_bytes() == DATA


def test_a_bad_kept_part_is_retried_from_zero_at_the_same_origin(
    tmp_path: Path, origin: FakeOrigin
) -> None:
    url = origin.add("a.bin", DATA)
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    (downloads / f"{DIGEST}.part").write_bytes(b"garbage!")
    assert fetch(tmp_path, origin, url, mirror="").read_bytes() == DATA
    assert [s.headers.get("range") for s in origin.seen] == ["bytes=8-", None]


def test_every_request_carries_the_timeout(tmp_path: Path, origin: FakeOrigin) -> None:
    url = origin.add("a.bin", DATA)
    origin.cut_once[url] = 10
    fetch(tmp_path, origin, url)
    assert origin.seen
    for seen in origin.seen:
        assert seen.timeout == {"connect": 7.5, "read": 7.5, "write": 7.5, "pool": 7.5}


def test_redirects_stay_on_https(tmp_path: Path, origin: FakeOrigin) -> None:
    url = origin.add("a.bin", DATA)
    hop = "https://dl.example/moved"
    origin.redirects[hop] = url
    assert fetch(tmp_path, origin, hop).read_bytes() == DATA
    plain = "https://dl.example/plain"
    origin.redirects[plain] = "http://dl.example/a.bin"
    with pytest.raises(FetchFailed, match="refusing non-https URL"):
        fetch(tmp_path / "other", origin, plain, mirror="")


def test_a_plain_http_url_is_refused(tmp_path: Path, origin: FakeOrigin) -> None:
    with pytest.raises(FetchFailed, match="non-https"):
        fetch(tmp_path, origin, "http://dl.example/a.bin", mirror="")
    assert origin.seen == []


def test_a_cached_blob_is_reused_without_a_request(tmp_path: Path, origin: FakeOrigin) -> None:
    url = origin.add("a.bin", DATA)
    fetch(tmp_path, origin, url)
    origin.seen.clear()
    assert fetch(tmp_path, origin, url).read_bytes() == DATA
    assert origin.seen == []


def test_a_bundle_folder_is_tried_before_the_network(tmp_path: Path, origin: FakeOrigin) -> None:
    bundle = tmp_path / "bundle"
    (bundle / "blobs").mkdir(parents=True)
    (bundle / "blobs" / DIGEST).write_bytes(DATA)
    blob = fetch(tmp_path, origin, "https://dl.example/never", local_dirs=[bundle])
    assert blob.read_bytes() == DATA
    assert origin.seen == []


def test_hash_url_pins_and_caches(tmp_path: Path, origin: FakeOrigin) -> None:
    url = origin.add("a.bin", DATA)
    fetcher = make(tmp_path, origin)
    try:
        assert fetcher.hash_url(url) == DIGEST
        assert (tmp_path / "downloads" / DIGEST).read_bytes() == DATA
        with pytest.raises(PmError, match="HTTP 404"):
            fetcher.hash_url("https://dl.example/missing")
    finally:
        fetcher.close()


def test_a_pin_without_a_full_hash_is_refused(tmp_path: Path, origin: FakeOrigin) -> None:
    with pytest.raises(PmError, match="full sha256"):
        fetch(tmp_path, origin, "https://dl.example/a.bin", digest="abc")
