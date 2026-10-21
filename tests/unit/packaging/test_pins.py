"""Bootstrap pins: pins.json, the rendered sections, the check and the updater (P23-02)."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]
PINS_JSON = ROOT / "scripts" / "install" / "pins.json"
INSTALL_SH = ROOT / "scripts" / "install.sh"


@pytest.fixture(scope="module")
def pins() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "indus_install_pins", ROOT / "scripts" / "install" / "pins.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _data() -> dict[str, Any]:
    return json.loads(PINS_JSON.read_text(encoding="utf-8"))


def _verified(pins: ModuleType) -> dict[str, Any]:
    data = copy.deepcopy(_data())
    for tool in ("uv", "git_for_windows"):
        for name, entry in data[tool]["targets"].items():
            entry["sha256"] = hashlib.sha256(name.encode()).hexdigest()
    data["provisional"] = False
    return data


def test_the_checked_in_pins_pass_the_check(pins: ModuleType) -> None:
    assert pins.problems(_data()) == []
    assert pins.check() == []


def test_every_target_is_present(pins: ModuleType) -> None:
    data = _data()
    assert set(data["uv"]["targets"]) == {
        "linux-x64",
        "linux-arm64",
        "linux-x64-musl",
        "linux-arm64-musl",
        "darwin-x64",
        "darwin-arm64",
        "windows-x64",
        "windows-arm64",
    }
    assert set(data["git_for_windows"]["targets"]) == {"windows-x64", "windows-arm64"}
    assert data["python"] == {"provision": "3.14", "floor": "3.13"}
    broken = copy.deepcopy(data)
    del broken["uv"]["targets"]["darwin-arm64"]
    assert "uv.targets.darwin-arm64 is missing" in pins.problems(broken)


def test_urls_are_https_and_mirrors_are_ours(pins: ModuleType) -> None:
    data = _data()
    base = f"https://github.com/{data['release']['repository']}/releases/download/"
    for tool in ("uv", "git_for_windows"):
        for entry in data[tool]["targets"].values():
            assert entry["url"].startswith("https://")
            assert entry["mirror"].startswith(base)
    plain = copy.deepcopy(data)
    plain["uv"]["targets"]["linux-x64"]["url"] = "http://example.com/uv.tar.gz"
    assert any("must be an https URL" in p for p in pins.problems(plain))
    foreign = copy.deepcopy(data)
    foreign["uv"]["targets"]["linux-x64"]["mirror"] = "https://elsewhere.example/uv.tar.gz"
    assert any("must be one of our release assets" in p for p in pins.problems(foreign))
    quoted = copy.deepcopy(data)
    quoted["uv"]["targets"]["linux-x64"]["url"] = "https://example.com/a'b"
    assert pins.problems(quoted)


def test_the_fragment_equals_the_json(pins: ModuleType) -> None:
    built = INSTALL_SH.read_text(encoding="utf-8")
    assert pins.section(built) == pins.render_sh(_data())
    rendered = pins.render_sh(_verified(pins))
    for target in ("linux-x64", "linux-arm64-musl", "darwin-arm64"):
        assert f"        {target})" in rendered
    assert "windows-x64" not in rendered  # the POSIX script has no Windows pins


def test_a_tampered_digest_in_the_script_fails_the_check(pins: ModuleType, tmp_path: Path) -> None:
    data = _verified(pins)
    pins_file = tmp_path / "pins.json"
    pins_file.write_text(json.dumps(data))
    script = tmp_path / "install.sh"
    script.write_text("#!/usr/bin/env bash\n" + pins.render_sh(data) + "ins_main\n")
    assert pins.check(pins_file, script) == []
    digest = data["uv"]["targets"]["linux-x64"]["sha256"]
    script.write_text(script.read_text().replace(digest, "0" * 64))
    (problem,) = pins.check(pins_file, script)
    assert "pins section differs" in problem


def test_a_tampered_digest_in_the_json_fails_the_check(pins: ModuleType, tmp_path: Path) -> None:
    data = _verified(pins)
    pins_file = tmp_path / "pins.json"
    script = tmp_path / "install.sh"
    script.write_text(pins.render_sh(data))
    data["uv"]["targets"]["darwin-x64"]["sha256"] = "f" * 64
    pins_file.write_text(json.dumps(data))
    assert pins.check(pins_file, script)
    data["uv"]["targets"]["darwin-x64"]["sha256"] = "F" * 64
    assert any("64 lowercase hex" in p for p in pins.problems(data))


def test_strict_mode_refuses_provisional_pins(pins: ModuleType) -> None:
    data = _data()
    if data["provisional"]:
        assert any("provisional" in p for p in pins.problems(data, strict=True))
    assert pins.problems(_verified(pins), strict=True) == []
    unmarked = copy.deepcopy(_verified(pins))
    unmarked["uv"]["targets"]["linux-x64"]["sha256"] = None
    assert any("not marked provisional" in p for p in pins.problems(unmarked))


def test_git_for_windows_asset_names(pins: ModuleType) -> None:
    assert pins.git_asset("2.51.0.windows.1", "windows-x64") == "PortableGit-2.51.0-64-bit.7z.exe"
    assert (
        pins.git_asset("2.53.0.windows.3", "windows-arm64") == "PortableGit-2.53.0.3-arm64.7z.exe"
    )


def test_update_computes_digests_from_downloads(pins: ModuleType) -> None:
    fetched: list[str] = []

    def fetch(url: str) -> bytes:
        fetched.append(url)
        return url.encode()

    new = pins.update(_data(), "0.12.0", "2.52.0.windows.1", fetch)
    assert new["provisional"] is False
    assert new["uv"]["version"] == "0.12.0"
    entry = new["uv"]["targets"]["linux-x64"]
    assert entry["url"].endswith("/0.12.0/uv-x86_64-unknown-linux-gnu.tar.gz")
    assert entry["sha256"] == hashlib.sha256(entry["url"].encode()).hexdigest()
    assert len(fetched) == 10
    assert pins.problems(new, strict=True) == []


def test_update_writes_nothing_when_a_download_fails(
    pins: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pins_file = tmp_path / "pins.json"
    pins_file.write_text(PINS_JSON.read_text())
    monkeypatch.setattr(pins, "PINS", pins_file)

    def fetch(url: str) -> bytes:
        raise OSError(f"cannot reach {url}")

    assert pins.main(["update", "--uv", "0.12.0"], fetch=fetch) == 1
    assert pins_file.read_text() == PINS_JSON.read_text()


def test_open_pr_branches_commits_and_opens_a_draft(pins: ModuleType) -> None:
    ran: list[list[str]] = []

    def run(argv: Sequence[str]) -> int:
        ran.append(list(argv))
        return 0

    assert pins.open_pr("0.12.0", "2.52.0.windows.1", run) == 0
    assert [step[:2] for step in ran] == [
        ["git", "switch"],
        ["git", "add"],
        ["git", "commit"],
        ["gh", "pr"],
    ]
    assert "--draft" in ran[-1]
    assert not any(step[:3] == ["gh", "pr", "merge"] or "--auto" in step for step in ran)

    def failing(argv: Sequence[str]) -> int:
        ran.append(list(argv))
        return 1 if argv[1] == "commit" else 0

    ran.clear()
    assert pins.open_pr("0.12.0", "2.52.0.windows.1", failing) == 1
    assert ran[-1][:2] == ["git", "commit"]


def test_cli_check(pins: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    assert pins.main(["--check"]) == 0
    assert "pins: ok" in capsys.readouterr().out
