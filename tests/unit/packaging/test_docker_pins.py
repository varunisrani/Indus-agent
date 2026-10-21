"""Pinned downloads for the images (P23-08, P23-09, P23-12): ``scripts/docker/pins.py``."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import shutil
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.config.schema import default_for
from tests.unit.packaging.docker_files import DOCKERFILE, ROOT, SANDBOX_DOCKERFILE, load_pins

pins = load_pins()

DIGEST = "sha256:" + "ab" * 32


def repo_pins() -> dict[str, Any]:
    return json.loads((ROOT / "docker" / "pins.json").read_text(encoding="utf-8"))


def make_root(tmp_path: Path, data: dict[str, Any] | None = None) -> Path:
    """A minimal repository: the pins, both Dockerfiles and the release base (copied from the real
    ones)."""
    root = tmp_path / "repo"
    (root / "docker" / "sandbox").mkdir(parents=True)
    versions = Path("src/indus/foundation/versions.py")
    (root / versions).parent.mkdir(parents=True)
    shutil.copy(ROOT / versions, root / versions)
    (root / "docker" / "pins.json").write_text(
        json.dumps(data if data is not None else repo_pins()), encoding="utf-8"
    )
    shutil.copy(DOCKERFILE, root / "docker" / "Dockerfile")
    shutil.copy(SANDBOX_DOCKERFILE, root / "docker" / "sandbox" / "Dockerfile")
    return root


class Recorder:
    """An opener that serves bytes per URL, or raises, and records every URL asked for."""

    def __init__(self, answers: dict[str, bytes | Exception]) -> None:
        self.answers = answers
        self.calls: list[str] = []

    def __call__(self, url: str, timeout: float) -> io.BytesIO:
        self.calls.append(url)
        answer = self.answers.get(url, urllib.error.URLError("no route"))
        if isinstance(answer, Exception):
            raise answer
        return io.BytesIO(answer)


def no_wait(seconds: float) -> None:
    return None


# -- the repository's pins ------------------------------------------------------------------------


def test_repository_pins_are_well_formed_and_the_dockerfiles_agree() -> None:
    assert pins.check(ROOT) == []


def test_every_bundle_has_both_architectures_and_https_urls() -> None:
    data = repo_pins()
    for name, bundle in data["bundles"].items():
        assert bundle["url"].startswith("https://"), name
        for arch in pins.ARCHES:
            artefacts = pins.bundle_artefacts(data, name, arch)
            assert artefacts and all(a.url.startswith("https://") for a in artefacts)
            assert artefacts[-1].name == f"{name}:{arch}"


def test_s6_takes_the_shared_parts_then_the_arch_tarball() -> None:
    names = [a.filename for a in pins.bundle_artefacts(repo_pins(), "s6-overlay", "arm64")]
    assert names == [
        "s6-overlay-noarch.tar.xz",
        "s6-overlay-symlinks-noarch.tar.xz",
        "s6-overlay-aarch64.tar.xz",
    ]


def test_the_schema_default_sandbox_image_is_the_pinned_one() -> None:
    expected = pins.image_name(repo_pins(), "sandbox") + ":stable-slim"
    assert default_for("terminal.docker_image") == expected


def test_image_command_prints_the_registry_path(capsys: pytest.CaptureFixture[str]) -> None:
    assert pins.main(["--root", str(ROOT), "image", "product"]) == 0
    images = repo_pins()["images"]
    assert capsys.readouterr().out.strip() == (
        f"{images['registry']}/{images['namespace']}/{images['product']}"
    )


def test_installer_uv_pin_resolves_for_both_arches_when_present() -> None:
    install = ROOT / "scripts" / "install" / "pins.json"
    if not install.is_file():
        pytest.skip("the installer pins (P23-02) are not in this tree yet")
    data = json.loads(install.read_text(encoding="utf-8"))
    for arch in pins.ARCHES:
        artefact = pins.uv_artefact(data, arch)
        assert artefact.url.startswith("https://")


# -- checking -------------------------------------------------------------------------------------


def test_strict_reports_every_empty_digest(tmp_path: Path) -> None:
    data = repo_pins()
    data["base"]["digest"] = None
    data["bundles"]["node"]["files"]["amd64"]["sha256"] = None
    root = make_root(tmp_path, data)
    pins.render(root)
    assert pins.check(root) == []
    found = pins.check(root, strict=True)
    assert any("base.digest is empty" in p for p in found)
    assert any("bundles.node.files.amd64.sha256 is empty" in p for p in found)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d["base"].__setitem__("digest", "sha256:XYZ"), "base.digest must be"),
        (lambda d: d["base"].__setitem__("tag", "latest"), "must not be latest"),
        (
            lambda d: d["bundles"]["node"].__setitem__("url", "http://example.test/{file}"),
            "https template",
        ),
        (lambda d: d["bundles"]["node"]["files"].pop("arm64"), "has no arm64 entry"),
        (
            lambda d: d["bundles"]["node"]["files"]["amd64"].__setitem__("sha256", "ABC"),
            "64 lowercase hex",
        ),
        (lambda d: d["images"].__setitem__("namespace", "Upper"), "lowercase registry name"),
    ],
)
def test_malformed_pins_are_reported(mutate: Any, message: str) -> None:
    data = copy.deepcopy(repo_pins())
    mutate(data)
    assert any(message in p for p in pins.problems(data)), pins.problems(data)


def test_a_dockerfile_off_the_pinned_base_fails_until_rendered(tmp_path: Path) -> None:
    data = repo_pins()
    data["base"]["digest"] = DIGEST
    root = make_root(tmp_path, data)
    found = pins.check(root)
    assert len(found) == 2 and all("run `pins.py render`" in p for p in found)
    assert sorted(str(p) for p in pins.render(root)) == [
        "docker/Dockerfile",
        "docker/sandbox/Dockerfile",
    ]
    assert pins.check(root) == []
    text = (root / "docker" / "Dockerfile").read_text(encoding="utf-8")
    assert f"ARG BASE_IMAGE=debian:{data['base']['tag']}@{DIGEST}" in text
    assert pins.render(root) == []


def test_check_command_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data = repo_pins()
    data["base"]["digest"] = None
    root = make_root(tmp_path, data)
    pins.render(root)
    assert pins.main(["--root", str(root), "check"]) == 0
    assert pins.main(["--root", str(root), "check", "--strict"]) == 1
    assert "run `pins.py refresh`" in capsys.readouterr().err


# -- downloading ----------------------------------------------------------------------------------


def artefact(url: str, payload: bytes, mirror: str | None = None) -> Any:
    return pins.Artefact("thing:amd64", url, hashlib.sha256(payload).hexdigest(), mirror)


def test_download_verifies_and_keeps_the_file(tmp_path: Path) -> None:
    payload = b"pinned bytes"
    opener = Recorder({"https://up.test/a.tar.xz": payload})
    out = tmp_path / "dl"
    path = pins.download(
        artefact("https://up.test/a.tar.xz", payload), out, opener=opener, sleeper=no_wait
    )
    assert path == out / "a.tar.xz" and path.read_bytes() == payload
    assert [p.name for p in out.iterdir()] == ["a.tar.xz"]


def test_digest_mismatch_fails_at_once_and_never_tries_the_mirror(tmp_path: Path) -> None:
    opener = Recorder({"https://up.test/a": b"tampered", "https://mirror.test/a": b"good"})
    item = artefact("https://up.test/a", b"good", mirror="https://mirror.test/a")
    with pytest.raises(pins.PinError, match="SHA-256 mismatch"):
        pins.download(item, tmp_path / "dl", opener=opener, sleeper=no_wait)
    assert opener.calls == ["https://up.test/a"]
    assert list((tmp_path / "dl").iterdir()) == []


def test_transport_failures_retry_then_use_the_mirror(tmp_path: Path) -> None:
    payload = b"good"
    waits: list[float] = []
    opener = Recorder(
        {
            "https://up.test/a": urllib.error.HTTPError("https://up.test/a", 502, "bad", {}, None),  # type: ignore[arg-type]
            "https://mirror.test/a": payload,
        }
    )
    item = artefact("https://up.test/a", payload, mirror="https://mirror.test/a")
    path = pins.download(item, tmp_path, opener=opener, sleeper=waits.append)
    assert path.read_bytes() == payload
    assert opener.calls == ["https://up.test/a"] * pins.ATTEMPTS + ["https://mirror.test/a"]
    assert waits == [2.0, 4.0]


def test_every_origin_failing_leaves_nothing(tmp_path: Path) -> None:
    item = artefact("https://up.test/a", b"x")
    with pytest.raises(pins.PinError, match="download failed"):
        pins.download(item, tmp_path / "dl", opener=Recorder({}), sleeper=no_wait)
    assert list((tmp_path / "dl").iterdir()) == []


def test_an_unpinned_artefact_is_refused_before_any_download(tmp_path: Path) -> None:
    opener = Recorder({})
    item = pins.Artefact("node:amd64", "https://up.test/node.tar.xz", None)
    with pytest.raises(pins.PinError, match="no SHA-256 pinned"):
        pins.download(item, tmp_path, opener=opener, sleeper=no_wait)
    assert opener.calls == []


def test_plain_http_is_refused(tmp_path: Path) -> None:
    item = pins.Artefact("x:amd64", "http://up.test/x", "0" * 64)
    with pytest.raises(pins.PinError, match="non-https"):
        pins.download(item, tmp_path, sleeper=no_wait)


def test_unknown_architecture_is_refused() -> None:
    with pytest.raises(pins.PinError, match="unsupported architecture"):
        pins.bundle_artefacts(repo_pins(), "node", "riscv64")


def write_payloads(tmp_path: Path, names: list[str]) -> dict[str, bytes]:
    served = tmp_path / "served"
    served.mkdir()
    out: dict[str, bytes] = {}
    for name in names:
        data = f"payload of {name}".encode()
        (served / name).write_bytes(data)
        out[name] = data
    return out


def file_pins(tmp_path: Path, payloads: dict[str, bytes], bad: str | None = None) -> Path:
    """Docker pins whose s6 bundle is served from ``file://`` (``bad``: a wrong digest)."""
    data = repo_pins()
    s6 = data["bundles"]["s6-overlay"]
    s6["url"] = (tmp_path / "served").as_uri() + "/{file}"
    for entry in s6["files"].values():
        payload = payloads.get(entry["file"], b"")
        entry["sha256"] = hashlib.sha256(payload).hexdigest()
        if entry["file"] == bad:
            entry["sha256"] = "0" * 64
    path = tmp_path / "docker-pins.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def install_pins(tmp_path: Path, payload: bytes) -> Path:
    """Installer pins in the shape of ``scripts/install/pins.json`` (P23-02)."""
    served = tmp_path / "served"
    served.mkdir(exist_ok=True)
    (served / "uv-x86_64-unknown-linux-gnu.tar.gz").write_bytes(payload)
    data = {
        "uv": {
            "version": "0.0.0-test",
            "targets": {
                "linux-x64": {
                    "url": (served / "uv-x86_64-unknown-linux-gnu.tar.gz").as_uri(),
                    "mirror": "https://mirror.test/uv.tar.gz",
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
            },
        }
    }
    path = tmp_path / "install-pins.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


S6_FILES = [
    "s6-overlay-noarch.tar.xz",
    "s6-overlay-symlinks-noarch.tar.xz",
    "s6-overlay-x86_64.tar.xz",
]


def test_fetch_downloads_a_bundle_and_uv(tmp_path: Path) -> None:
    payloads = write_payloads(tmp_path, S6_FILES)
    docker = file_pins(tmp_path, payloads)
    install = install_pins(tmp_path, b"uv archive")
    out = tmp_path / "out"
    code = pins.main(
        [
            "fetch",
            "s6-overlay",
            "uv",
            "--arch",
            "amd64",
            "--out",
            str(out),
            "--docker-pins",
            str(docker),
            "--install-pins",
            str(install),
        ]
    )
    assert code == 0
    assert sorted(p.name for p in out.iterdir()) == sorted(
        [*S6_FILES, "uv-x86_64-unknown-linux-gnu.tar.gz"]
    )


def test_fetch_removes_what_it_wrote_when_a_later_file_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    payloads = write_payloads(tmp_path, S6_FILES)
    docker = file_pins(tmp_path, payloads, bad="s6-overlay-x86_64.tar.xz")
    out = tmp_path / "out"
    args = [
        "fetch",
        "s6-overlay",
        "--arch",
        "amd64",
        "--out",
        str(out),
        "--docker-pins",
        str(docker),
    ]
    assert pins.main(args) == 1
    assert "SHA-256 mismatch" in capsys.readouterr().err
    assert list(out.iterdir()) == []


def test_uv_target_missing_for_an_arch_is_an_error(tmp_path: Path) -> None:
    data = json.loads(install_pins(tmp_path, b"x").read_text(encoding="utf-8"))
    with pytest.raises(pins.PinError, match="no uv target for linux arm64"):
        pins.uv_artefact(data, "arm64")


# -- refreshing -----------------------------------------------------------------------------------


class FakeResponse(io.BytesIO):
    def __init__(self, body: bytes = b"", headers: dict[str, str] | None = None) -> None:
        super().__init__(body)
        self.headers = headers or {}


def test_refresh_fills_every_digest_and_renders(tmp_path: Path) -> None:
    data = repo_pins()
    data["base"]["digest"] = None
    root = make_root(tmp_path, data)
    seen: list[str] = []

    def request_opener(request: Any) -> FakeResponse:
        seen.append(request.full_url)
        if "auth.docker.io" in request.full_url:
            return FakeResponse(json.dumps({"token": "t"}).encode())
        assert request.get_method() == "HEAD"
        return FakeResponse(headers={"Docker-Content-Digest": DIGEST})

    def opener(url: str, timeout: float) -> io.BytesIO:
        return io.BytesIO(url.encode())

    refreshed = pins.refresh(root, opener=opener, request_opener=request_opener)
    assert refreshed["base"]["digest"] == DIGEST
    assert pins.check(root, strict=True) == []
    node = refreshed["bundles"]["node"]
    url = node["url"].format(
        version=node["version"],
        file=node["files"]["arm64"]["file"].format(version=node["version"]),
    )
    assert node["files"]["arm64"]["sha256"] == hashlib.sha256(url.encode()).hexdigest()
    assert any("library/debian" in u for u in seen)
