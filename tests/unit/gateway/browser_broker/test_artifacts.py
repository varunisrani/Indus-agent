"""P24-47: the artefact store — server-minted ids, 10 MiB cap, MIME allow-list, 300 s TTL,
SHA-256 checked again on read, temp-then-rename, consumed once with the exact scope key."""

from __future__ import annotations

import os
import stat
import threading
from pathlib import Path

import pytest

from indus.gateway.browser_broker import artifacts as art
from indus.gateway.browser_broker.artifacts import ArtifactRefused, ArtifactStore

KEY = "a" * 64
OTHER = "b" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"pixels" * 100


class Mono:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def mono() -> Mono:
    return Mono()


@pytest.fixture
def store(tmp_path: Path, mono: Mono) -> ArtifactStore:
    return ArtifactStore(tmp_path / "artifacts", monotonic=mono)


def reason(call: object) -> str:
    try:
        call()  # type: ignore[operator]
    except ArtifactRefused as refused:
        return refused.reason
    raise AssertionError("not refused")


def test_put_then_consume_once(store: ArtifactStore) -> None:
    receipt = store.put(KEY, PNG, "image/png")
    assert art.ARTIFACT_ID.match(receipt.artifact_id)
    assert receipt.size == len(PNG) and receipt.mime == "image/png" and len(receipt.sha256) == 64
    path = store.root / f"{receipt.artifact_id}.bin"
    assert path.read_bytes() == PNG
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [p.name for p in store.root.iterdir()] == [path.name]  # no temp file left
    assert store.check(receipt.artifact_id, KEY) == receipt
    data, again = store.consume(receipt.artifact_id, KEY)
    assert data == PNG and again == receipt
    assert not path.exists() and len(store) == 0
    assert reason(lambda: store.consume(receipt.artifact_id, KEY)) == "unknown"


def test_ids_are_fresh(store: ArtifactStore) -> None:
    ids = {store.put(KEY, b"x", "text/plain").artifact_id for _ in range(20)}
    assert len(ids) == 20


def test_size_cap(store: ArtifactStore) -> None:
    exactly = b"\0" * art.MAX_BYTES
    assert store.put(KEY, exactly, "application/pdf").size == art.MAX_BYTES
    assert reason(lambda: store.put(KEY, b"\0" * (11 * 1024 * 1024), "application/pdf")) == "size"
    assert reason(lambda: store.put(KEY, exactly + b"\0", "application/pdf")) == "size"
    assert len(store) == 1


@pytest.mark.parametrize("mime", ["text/html", "application/javascript", "image/svg+xml", ""])
def test_mime_allow_list(store: ArtifactStore, mime: str) -> None:
    assert reason(lambda: store.put(KEY, b"x", mime)) == "mime"


def test_mime_parameters_and_case_are_normalised(store: ArtifactStore) -> None:
    assert store.put(KEY, b"a,b", "Text/CSV; charset=utf-8").mime == "text/csv"


def test_bytes_changed_on_disk_fail_the_check(store: ArtifactStore) -> None:
    receipt = store.put(KEY, PNG, "image/png")
    path = store.root / f"{receipt.artifact_id}.bin"
    path.write_bytes(PNG[:-1] + b"!")  # same size, other bytes
    assert reason(lambda: store.consume(receipt.artifact_id, KEY)) == "changed"
    assert not path.exists()
    assert reason(lambda: store.consume(receipt.artifact_id, KEY)) == "unknown"


def test_scope_key_must_match_exactly(store: ArtifactStore) -> None:
    receipt = store.put(KEY, PNG, "image/png")
    assert reason(lambda: store.check(receipt.artifact_id, OTHER)) == "scope"
    assert reason(lambda: store.consume(receipt.artifact_id, OTHER)) == "scope"
    assert reason(lambda: store.consume(receipt.artifact_id, KEY[:-1])) == "scope"
    assert store.consume(receipt.artifact_id, KEY)[0] == PNG  # a refusal consumed nothing
    assert reason(lambda: store.put("", PNG, "image/png")) == "scope"


def test_lifetime(store: ArtifactStore, mono: Mono) -> None:
    receipt = store.put(KEY, PNG, "image/png")
    mono.now += art.TTL_S - 1
    assert store.check(receipt.artifact_id, KEY)
    mono.now += 1
    assert reason(lambda: store.check(receipt.artifact_id, KEY)) == "unknown"
    assert not (store.root / f"{receipt.artifact_id}.bin").exists()
    kept = store.put(KEY, PNG, "image/png")
    mono.now += art.TTL_S
    assert store.sweep() == 1 and not any(store.root.iterdir())
    assert reason(lambda: store.consume(kept.artifact_id, KEY)) == "unknown"


@pytest.mark.parametrize(
    "bad", ["../../etc/passwd", "A" * 32, "0" * 31, "0" * 33, "", None, 7, "0" * 31 + "g"]
)
def test_only_server_shaped_ids(store: ArtifactStore, bad: object) -> None:
    assert reason(lambda: store.check(bad, KEY)) == "malformed"


def test_one_of_many_concurrent_consumers_wins(store: ArtifactStore) -> None:
    receipt = store.put(KEY, PNG, "image/png")
    got: list[bytes] = []
    refused: list[str] = []

    def take() -> None:
        try:
            got.append(store.consume(receipt.artifact_id, KEY)[0])
        except ArtifactRefused as exc:
            refused.append(exc.reason)

    threads = [threading.Thread(target=take) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert got == [PNG] and refused == ["unknown"] * 7


def test_a_failed_write_leaves_no_temp_file(
    store: ArtifactStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(src: object, dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        store.put(KEY, PNG, "image/png")
    assert list(store.root.iterdir()) == [] and len(store) == 0


def test_a_missing_file_is_refused(store: ArtifactStore) -> None:
    receipt = store.put(KEY, PNG, "image/png")
    (store.root / f"{receipt.artifact_id}.bin").unlink()
    assert reason(lambda: store.consume(receipt.artifact_id, KEY)) == "missing"


def test_clear(store: ArtifactStore) -> None:
    store.put(KEY, PNG, "image/png")
    store.put(OTHER, b"{}", "application/json")
    store.clear()
    assert len(store) == 0 and not any(store.root.iterdir())
