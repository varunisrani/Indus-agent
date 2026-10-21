"""P25-19: the clean-room sweep over built artefacts (ASAR, image tars, zips, folders).

Banned names here come from a test pattern file (invented words), never the real list.
"""

from __future__ import annotations

import io
import json
import struct
import tarfile
import zipfile
from pathlib import Path

import pytest

from tooling.checks import artifacts
from tooling.checks.common import Context

PATTERNS = [
    {"pattern": r"\blanterncorp\b", "hint": "reference name: use indus", "flags": ["IGNORECASE"]},
    {"pattern": r"\bharbour\.example\b", "hint": "reference vendor host: drop"},
]
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(64))  # invented "reference" image bytes


def asar(files: dict[str, bytes]) -> bytes:
    """A minimal ASAR archive (Chromium pickle header + JSON index + file data)."""
    tree: dict[str, object] = {"files": {}}
    blob = b""
    for path, data in files.items():
        node = tree
        *dirs, name = path.split("/")
        for d in dirs:
            node = node["files"].setdefault(d, {"files": {}})  # type: ignore[index,union-attr]
        node["files"][name] = {"size": len(data), "offset": str(len(blob))}  # type: ignore[index]
        blob += data
    header = json.dumps(tree).encode("utf-8")
    payload = struct.pack("<I", len(header)) + header
    payload += b"\0" * (-len(payload) % 4)
    pickle = struct.pack("<I", len(payload)) + payload
    return struct.pack("<II", 4, len(pickle)) + pickle + blob


def tar_of(files: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return out.getvalue()


@pytest.fixture
def patterns(tmp_path: Path) -> Path:
    path = tmp_path / "patterns.json"
    path.write_text(json.dumps(PATTERNS), encoding="utf-8")
    return path


@pytest.fixture
def no_allow(tmp_path: Path) -> Path:
    path = tmp_path / "allow.json"
    path.write_text("[]", encoding="utf-8")
    return path


@pytest.fixture
def clean_release(tmp_path: Path) -> Path:
    """An unpacked desktop release: a binary, resources/app.asar, an unpacked native module."""
    root = tmp_path / "linux-unpacked"
    (root / "resources" / "app.asar.unpacked").mkdir(parents=True)
    (root / "indus-desktop").write_bytes(b"\x7fELF\0\0binary")
    (root / "resources" / "app.asar").write_bytes(
        asar(
            {
                "package.json": b'{"name": "indus-desktop"}',
                "dist/index.js": b"console.log('indus');\n",
                "dist/assets/logo.png": b"\x89PNG\r\n\x1a\nour own pixels",
            }
        )
    )
    (root / "resources" / "app.asar.unpacked" / "pty.node").write_bytes(b"\0native")
    return root


def sweep(
    paths: list[Path], patterns: Path, allow: Path, ref: Path | None = None
) -> artifacts.Result:
    ctx = Context(ref=ref, artifacts=tuple(str(p) for p in paths))
    return artifacts.run(ctx, patterns_file=patterns, allow_path=allow)


def test_the_asar_reader_round_trips() -> None:
    data = asar({"a.txt": b"one", "dir/b.js": b"two", "dir/deeper/c.css": b"three"})
    assert dict(artifacts.read_asar(data)) == {
        "a.txt": b"one",
        "dir/b.js": b"two",
        "dir/deeper/c.css": b"three",
    }
    with pytest.raises(artifacts.AsarError):
        list(artifacts.read_asar(b"not an archive at all"))


def test_a_clean_build_passes(clean_release: Path, patterns: Path, no_allow: Path) -> None:
    result = sweep([clean_release], patterns, no_allow)
    assert result.code == 0, result.violations
    assert result.data["members"] == 6  # three files on disk and three inside the ASAR


def test_a_seeded_banned_name_inside_an_asar_fails(
    clean_release: Path, patterns: Path, no_allow: Path
) -> None:
    (clean_release / "resources" / "app.asar").write_bytes(
        asar({"dist/index.js": b"const vendor = 'LanternCorp';\n"})
    )
    result = sweep([clean_release], patterns, no_allow)
    assert result.code == 1
    assert result.violations == [
        "linux-unpacked/resources/app.asar!dist/index.js:1:17: 'LanternCorp'; "
        "reference name: use indus"
    ]


def test_a_banned_name_in_a_member_path_fails(
    tmp_path: Path, patterns: Path, no_allow: Path
) -> None:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "assets" / "lanterncorp-logo.svg").write_text("<svg/>", encoding="utf-8")
    found = "\n".join(sweep([dist], patterns, no_allow).violations)
    assert "dist/assets/lanterncorp-logo.svg: path contains 'lanterncorp'" in found


def test_a_reference_host_in_the_web_dist_fails(
    tmp_path: Path, patterns: Path, no_allow: Path
) -> None:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "assets" / "index-a1.js").write_text(
        'fetch("https://api.harbour.example/v1")', encoding="utf-8"
    )
    found = "\n".join(sweep([dist], patterns, no_allow).violations)
    assert "dist/assets/index-a1.js:1:" in found and "'harbour.example'" in found


def test_a_docker_image_tar_is_swept_layer_by_layer(
    tmp_path: Path, patterns: Path, no_allow: Path
) -> None:
    layer = tar_of({"opt/indus/app.py": b"print('ok')\n", "etc/motd": b"made by lanterncorp\n"})
    config = json.dumps({"config": {"Labels": {"org.opencontainers.image.vendor": "Indus"}}})
    image = tar_of(
        {
            "manifest.json": b'[{"Config": "config.json", "Layers": ["abc/layer.tar"]}]',
            "config.json": config.encode(),
            "abc/layer.tar": layer,
        }
    )
    path = tmp_path / "indus-image.tar"
    path.write_bytes(image)
    result = sweep([path], patterns, no_allow)
    assert result.violations == [
        "indus-image.tar!abc/layer.tar!etc/motd:1:9: 'lanterncorp'; reference name: use indus"
    ]


def test_image_labels_are_checked(tmp_path: Path, patterns: Path, no_allow: Path) -> None:
    config = json.dumps({"config": {"Labels": {"org.opencontainers.image.url": "harbour.example"}}})
    path = tmp_path / "image.tar"
    path.write_bytes(tar_of({"config.json": config.encode()}))
    assert "image.tar!config.json:1:" in "\n".join(sweep([path], patterns, no_allow).violations)


def test_zip_installers_are_opened(tmp_path: Path, patterns: Path, no_allow: Path) -> None:
    path = tmp_path / "Indus-Setup.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("install.ps1", "# LanternCorp installer\n")
    assert "Indus-Setup.zip!install.ps1:1:3:" in "\n".join(
        sweep([path], patterns, no_allow).violations
    )


def test_a_reference_asset_inside_an_asar_fails(
    clean_release: Path, patterns: Path, no_allow: Path, tmp_path: Path, tiny_cache: Path
) -> None:
    ref = tmp_path / "ref"
    (ref / "assets").mkdir(parents=True)
    (ref / "assets" / "mark.png").write_bytes(PNG)
    (clean_release / "resources" / "app.asar").write_bytes(asar({"dist/assets/mark.png": PNG}))
    found = "\n".join(sweep([clean_release], patterns, no_allow, ref=ref).violations)
    assert "app.asar!dist/assets/mark.png is identical to reference assets/mark.png" in found


def test_the_build_machines_home_folder_is_personal_data(
    tmp_path: Path, patterns: Path, no_allow: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home" / "builder"
    home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.js.map").write_text(json.dumps({"sources": [f"{home}/src/app.ts"]}), "utf-8")
    found = "\n".join(sweep([dist], patterns, no_allow).violations)
    assert "dist/index.js.map:1:" in found and "personal data" in found


def test_the_allow_list_skips_named_members_with_a_reason(tmp_path: Path, patterns: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "NOTICE.txt").write_text("Portions credit LanternCorp (MIT)\n", encoding="utf-8")
    allow = tmp_path / "allow.json"
    allow.write_text(
        json.dumps([{"path": "dist/NOTICE.txt", "reason": "licence credit"}]), encoding="utf-8"
    )
    result = sweep([dist], patterns, allow)
    assert result.code == 0 and result.data["allowed"] == 1
    allow.write_text(json.dumps([{"path": "dist/NOTICE.txt"}]), encoding="utf-8")
    assert sweep([dist], patterns, allow).code == 2  # every entry needs a reason


def test_nothing_named_is_a_note_and_a_missing_artefact_is_setup(
    tmp_path: Path, patterns: Path, no_allow: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(artifacts.ENV, raising=False)
    quiet = sweep([], patterns, no_allow)
    assert quiet.code == 0 and "nothing swept" in quiet.notes[0]
    assert sweep([tmp_path / "missing"], patterns, no_allow).code == 2


def test_artefacts_can_come_from_the_environment(
    clean_release: Path, patterns: Path, no_allow: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(artifacts.ENV, str(clean_release))
    result = artifacts.run(Context(), patterns_file=patterns, allow_path=no_allow)
    assert result.data["artifacts"] == [str(clean_release)]
