"""Plugin packs (P24-44): parsing, pins, seeds, the review, the fan-out and export, against local
bare git repos (``file://``) and the fixture packs in ``tests/fixtures/plugins/packs/``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from indus.foundation.config import loader
from indus.foundation.scope import Scope
from indus.plugins import packs
from indus.plugins.install import Installer
from indus.plugins.packs import PackError, apply_seed, export_pack, load_pack, parse_pack, resolve
from tests.unit.cli.commands._helpers_d import run
from tests.unit.plugins._git_repos import DANGEROUS_INIT, PluginRepo, git_scope, plugin_files

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "plugins" / "packs"


@pytest.fixture
def gscope(scope: Scope, tmp_path: Path) -> Scope:
    return git_scope(scope, tmp_path)


# -- parsing --------------------------------------------------------------------------------------


def test_valid_pack_parses_and_resolves() -> None:
    pack, warnings = load_pack(str(FIXTURES / "valid.yaml"))
    assert warnings == []
    assert (pack.name, pack.version, pack.author) == ("research-kit", "1.2.0", "Indus tests")
    first, second, third = pack.plugins
    assert first.identifier == "someone/indus-plugin-notes"
    assert second.identifier == "https://git.example.test/team/mono.git#plugins/cite"
    assert second.ref == second.ref.lower()
    assert third.identifier is None
    assert pack.config["notes"] == {"greeting": "hello", "limits": {"per_day": 5}}
    assert pack.skills == ("arxiv-reader",)
    rows = resolve(pack)
    assert [r.identifier is not None for r in rows] == [True, True, False]
    lines = packs.review_lines(pack, rows, warnings)
    text = "\n".join(lines)
    assert "Pack: research-kit 1.2.0 by Indus tests" in text
    assert "cannot install" in text and "plugins.entries.notes.greeting" in text
    assert "arxiv-reader" in text and "grants nothing" in text


def test_short_sha_is_refused_naming_the_entry() -> None:
    with pytest.raises(PackError) as caught:
        load_pack(str(FIXTURES / "short-sha.yaml"))
    assert "'someone/indus-plugin-cite'" in str(caught.value)
    assert "40-character" in str(caught.value)


@pytest.mark.parametrize("ref", ["main", "v1.2.0", "0123456", "0123456789abcdef" * 3])
def test_only_full_shas(ref: str) -> None:
    with pytest.raises(PackError):
        parse_pack(f"name: p\nplugins:\n  - repo: a/b\n    ref: {ref}\n")


@pytest.mark.parametrize(
    "text",
    [
        "- just a list",
        "name: p\nplugins: []\n",
        "plugins:\n  - repo: a/b\n    ref: " + "a" * 40 + "\n",
        "name: p\nplugins:\n  - ref: " + "a" * 40 + "\n",
        "name: p\nplugins:\n  - repo: a/b\n    ref: " + "a" * 40 + "\n    subdir: ../x\n",
        "name: p\nplugins:\n  - repo: a/b\n    ref: " + "a" * 40 + "\nconfig: [1]\n",
        "name: [unclosed\n",
    ],
)
def test_unusable_packs(text: str) -> None:
    with pytest.raises(PackError):
        parse_pack(text)


def test_secret_shaped_seeds_are_stripped_with_a_warning() -> None:
    pack, warnings = load_pack(str(FIXTURES / "secret-seed.yaml"))
    assert pack.config["notes"] == {"greeting": "hello", "nested": {"colour": "blue"}}
    joined = "\n".join(warnings)
    for key in ("api_key", "allow_network", "granted_capabilities", "nested.client_secret"):
        assert f"config.notes.{key} stripped" in joined
    assert "not-a-real-value" not in joined and "also-not-real" not in joined


def test_user_values_win_over_seeds() -> None:
    tree: dict[str, Any] = {"plugins": {"entries": {"notes": {"greeting": "mine"}}}}
    lines = apply_seed(tree, "notes", {"greeting": "hello", "colour": "blue", "token": "x"})
    assert tree["plugins"]["entries"]["notes"] == {"greeting": "mine", "colour": "blue"}
    assert any("your value is kept" in line for line in lines)
    assert any("token stripped" in line for line in lines)


def test_urls_must_be_https() -> None:
    body = ("name: p\nplugins:\n  - repo: a/b\n    ref: " + "c" * 40 + "\n").encode()
    pack, _ = load_pack("https://packs.example.test/p.yaml", fetch=lambda _url: body)
    assert pack.name == "p"
    with pytest.raises(PackError, match="https"):
        load_pack("http://packs.example.test/p.yaml")
    with pytest.raises(PackError, match="1 MiB"):
        load_pack("https://x.test/p.yaml", fetch=lambda _url: b"#" * (packs.MAX_PACK_BYTES + 1))


# -- the command ----------------------------------------------------------------------------------


def _pack_file(tmp_path: Path, *entries: tuple[str, str]) -> Path:
    lines = ["name: team-kit", "plugins:"]
    for url, ref in entries:
        lines += [f"  - repo: {url}", f"    ref: {ref}"]
    lines += ["config:", "  hello:", "    greeting: hi", "    api_key: nope"]
    path = tmp_path / "team-kit.yaml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_without_a_terminal_install_is_refused_after_the_review(
    gscope: Scope, tmp_path: Path
) -> None:
    repo = PluginRepo(tmp_path / "src")
    sha = repo.commit(plugin_files())
    path = _pack_file(tmp_path, (repo.url, sha))
    result = run(gscope, "plugins", "pack", "install", str(path))
    assert result.code == 1
    assert "Pack: team-kit" in result.out
    assert "needs a terminal" in result.err and "no --yes" in result.err
    assert Installer(gscope).records() == {}
    assert run(gscope, "plugins", "pack", "install", str(path), "--yes").code == 2


def test_show_is_a_dry_run(gscope: Scope) -> None:
    result = run(gscope, "plugins", "pack", "show", str(FIXTURES / "valid.yaml"))
    assert result.code == 0 and "1 entry cannot be installed" in result.out
    assert Installer(gscope).records() == {}


def test_one_failing_plugin_does_not_stop_the_others(gscope: Scope, tmp_path: Path) -> None:
    good = PluginRepo(tmp_path / "src", "hello")
    good_sha = good.commit(plugin_files("hello"))
    bad = PluginRepo(tmp_path / "src", "wiper")
    bad_sha = bad.commit(plugin_files("wiper", init=DANGEROUS_INIT))
    path = _pack_file(tmp_path, (bad.url, bad_sha), (good.url, good_sha))
    result = run(gscope, "plugins", "pack", "install", str(path), tty=True, input="y\n")
    assert result.code == 1, result.out + result.err
    assert "1 installed, 1 failed" in result.out
    assert "wiper" in result.err or "wiper" in result.out
    records = Installer(gscope).records()
    assert set(records) == {"hello"} and records["hello"]["revision"] == good_sha
    assert records["hello"]["pinned"] is True
    view = loader.load(gscope)
    assert "hello" in (view.get("plugins.enabled") or [])
    assert view.get("plugins.entries.hello.greeting") == "hi"
    assert view.get("plugins.entries.hello.api_key") is None


def test_declining_the_review_installs_nothing(gscope: Scope, tmp_path: Path) -> None:
    repo = PluginRepo(tmp_path / "src")
    path = _pack_file(tmp_path, (repo.url, repo.commit(plugin_files())))
    result = run(gscope, "plugins", "pack", "install", str(path), tty=True, input="n\n")
    assert result.code == 1 and "Nothing installed." in result.out
    assert Installer(gscope).records() == {}


# -- export ---------------------------------------------------------------------------------------


def test_export_keeps_git_installs_only(gscope: Scope, tmp_path: Path) -> None:
    repo = PluginRepo(tmp_path / "src")
    sha = repo.commit(plugin_files("hello"))
    Installer(gscope).install(repo.url, ref=sha)
    local = tmp_path / "local-plugin"
    local.mkdir()
    (local / "plugin.yaml").write_text("name: mine\nversion: 0.1.0\n", encoding="utf-8")
    (local / "__init__.py").write_text("def register(ctx):\n    pass\n", encoding="utf-8")
    Installer(gscope).install(str(local))
    tree = {"plugins": {"entries": {"hello": {"greeting": "hi", "auth_header": "x"}}}}
    text, warnings = export_pack(gscope, tree, name="mine-exported")
    assert any(w.startswith("mine:") for w in warnings)
    pack, pack_warnings = parse_pack(text)
    assert pack.name == "mine-exported" and pack_warnings == []
    (entry,) = pack.plugins
    assert entry.repo == repo.url and entry.ref == sha
    assert pack.config == {"hello": {"greeting": "hi"}}
    cli = run(gscope, "plugins", "pack", "export")
    assert cli.code == 0 and f"ref: {sha}" in cli.out and "# not exported: mine" in cli.out


def test_export_with_nothing_from_git(gscope: Scope) -> None:
    result = run(gscope, "plugins", "pack", "export")
    assert result.code == 1 and "Nothing to export" in result.err
