"""P13-21: plugin install / update / remove from local bare git repos (R07.19)."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.scope import Scope
from indus.plugins import install as inst
from indus.plugins.install import GitResult, InstallBlocked, Installer, InstallError
from tests.fakes.agent import DictConfig
from tests.unit.plugins._git_repos import (
    CAUTION_INIT,
    DANGEROUS_INIT,
    PluginRepo,
    git_scope,
    plugin_files,
)


@pytest.fixture
def repo(tmp_path: Path) -> PluginRepo:
    return PluginRepo(tmp_path / "src")


@pytest.fixture
def gscope(scope: Scope, tmp_path: Path) -> Scope:
    return git_scope(scope, tmp_path)


def installer(scope: Scope, **git: Any) -> Installer:
    return Installer(scope, git=inst.Git(scope, **git)) if git else Installer(scope)


# -- identifiers ---------------------------------------------------------------------------------


def test_ref_must_be_full_sha() -> None:
    for bad in ("abc1234", "v1.0.0", "main", "g" * 40, "a" * 39):
        with pytest.raises(InstallError, match="full 40-character commit SHA"):
            inst.full_sha(bad)
    assert inst.full_sha("A" * 40) == "a" * 40


def test_parse_source(tmp_path: Path) -> None:
    src = inst.parse_source("someone/indus-hello")
    assert (src.kind, src.url, src.subdir) == (
        "github",
        "https://github.com/someone/indus-hello.git",
        "",
    )
    deep = inst.parse_source("someone/mono/plugins/hello")
    assert deep.url == "https://github.com/someone/mono.git" and deep.subdir == "plugins/hello"
    custom = inst.parse_source("https://git.example.com/team/p.git#sub/dir")
    assert custom.custom and custom.subdir == "sub/dir"
    assert inst.parse_source("git@example.com:team/p.git").kind == "git"
    local = tmp_path / "mine"
    local.mkdir()
    assert inst.parse_source("./mine", cwd=tmp_path).kind == "local"
    with pytest.raises(InstallError, match="leaves the repository"):
        inst.parse_source("https://h/r.git#../../etc")
    with pytest.raises(InstallError):
        inst.parse_source("not a thing at all")


def test_scrub_url() -> None:
    secret = "pa" + "ss"
    assert inst.scrub_url(f"https://user:{secret}@host.example:8443/x.git?q=1#f") == (
        "https://host.example:8443/x.git"
    )
    assert inst.scrub_url("ssh://git@host/x.git") == "ssh://git@host/x.git"


# -- install ------------------------------------------------------------------------------------


def test_pin_installs_the_exact_commit(gscope: Scope, repo: PluginRepo) -> None:
    first = repo.commit(plugin_files(init="def register(ctx):\n    pass  # v1\n"))
    repo.commit({"__init__.py": "def register(ctx):\n    pass  # v2\n"})
    result = installer(gscope).install(repo.url, ref=first.upper())
    assert result.name == "hello"
    assert "# v1" in (result.target / "__init__.py").read_text(encoding="utf-8")
    record = Installer(gscope).record("hello")
    assert record is not None and record["pinned"] is True and record["revision"] == first
    assert record["kind"] == "git" and record["scan"]["verdict"] == "safe"
    assert not any(p.name.startswith(".staging-") for p in (gscope.home / "plugins").iterdir())


def test_pin_mismatch_refused(
    gscope: Scope, repo: PluginRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = repo.commit(plugin_files())
    monkeypatch.setattr(inst.Git, "head", lambda self, path: "f" * 40)
    with pytest.raises(InstallError, match="not the pinned commit"):
        installer(gscope).install(repo.url, ref=first)
    assert not (gscope.home / "plugins" / "hello").exists()
    assert Installer(gscope).records() == {}


def test_pinned_update_refused(gscope: Scope, repo: PluginRepo) -> None:
    first = repo.commit(plugin_files())
    repo.commit({"extra.txt": "more\n"})
    tool = installer(gscope)
    tool.install(repo.url, ref=first)
    with pytest.raises(InstallError, match="pinned"):
        tool.update("hello")
    # A reinstall without --ref stays on the pin; moving it takes an explicit --ref.
    kept = tool.install(repo.url, force=True)
    assert kept.record["pinned"] is True and kept.record["revision"] == first
    assert not (kept.target / "extra.txt").exists()
    tool.remove("hello")
    again = tool.install(repo.url)  # the record is gone with the plugin: a fresh install
    assert again.record["pinned"] is False and (again.target / "extra.txt").exists()
    tool.install(repo.url, force=True, ref=first)
    moved = tool.install(repo.url, force=True, ref=repo.git(repo.work, "rev-parse", "HEAD"))
    assert (moved.target / "extra.txt").exists()


def test_existing_plugin_needs_force(gscope: Scope, repo: PluginRepo) -> None:
    repo.commit(plugin_files())
    tool = installer(gscope)
    tool.install(repo.url)
    with pytest.raises(InstallError, match="already installed"):
        tool.install(repo.url)


def header_of(env: Mapping[str, str]) -> tuple[str, str] | None:
    for key, value in env.items():
        if key.startswith("GIT_CONFIG_KEY_") and value.endswith(".extraHeader"):
            return value, env.get(key.replace("_KEY_", "_VALUE_"), "")
    return None


class Recorder:
    """A git runner that refuses anonymous access to the GitHub URL, then serves it from the local
    bare repo; every call's argv and env are kept."""

    def __init__(self, local_url: str) -> None:
        self.local_url = local_url
        self.calls: list[tuple[list[str], dict[str, str]]] = []

    def __call__(
        self,
        argv: Sequence[str],
        cwd: Path,
        env: Mapping[str, str],
        timeout: float,
        stdin: str | None,
    ) -> GitResult:
        self.calls.append((list(argv), dict(env)))
        remote = "https://github.com/someone/hello.git"
        if remote in argv and argv[1] == "clone":
            if not header_of(env):
                return GitResult(128, "", f"fatal: Authentication failed for '{remote}'")
            argv = [self.local_url if a == remote else a for a in argv]
        return inst.run_process(argv, cwd, env, timeout, stdin)


def test_token_only_in_one_command_env(gscope: Scope, repo: PluginRepo) -> None:
    repo.commit(plugin_files())
    token = "tok" + "en-for-test-123"
    scoped = gscope.with_env(gscope.env.with_overlay({"GITHUB_TOKEN": token}))
    recorder = Recorder(repo.url)
    result = Installer(scoped, git=inst.Git(scoped, runner=recorder)).install("someone/hello")
    with_header = [(argv, header_of(env)) for argv, env in recorder.calls if header_of(env)]
    assert len(with_header) == 1
    argv, header = with_header[0]
    assert "clone" in argv and header is not None
    assert header[0] == "http.https://github.com/.extraHeader"
    assert header[1].startswith("Authorization: Basic ")
    for argv, _env in recorder.calls:
        assert not any(token in a for a in argv)  # never on a command line
    config = (result.target / ".git" / "config").read_text(encoding="utf-8")
    assert token not in config and "extraHeader" not in config and "Authorization" not in config
    assert token not in (gscope.home / "plugins" / inst.METADATA_FILE).read_text(encoding="utf-8")


def test_dangerous_blocked_even_with_force(gscope: Scope, repo: PluginRepo) -> None:
    repo.commit(plugin_files(init=DANGEROUS_INIT))
    with pytest.raises(InstallBlocked, match="dangerous") as caught:
        installer(gscope).install(repo.url, force=True, confirm=lambda report: True)
    assert caught.value.report is not None and caught.value.report.verdict == "dangerous"
    assert not (gscope.home / "plugins" / "hello").exists()
    assert Installer(gscope).records() == {}


def test_caution_needs_confirm(gscope: Scope, repo: PluginRepo) -> None:
    repo.commit(plugin_files(init=CAUTION_INIT))
    tool = installer(gscope)
    with pytest.raises(InstallBlocked, match="caution"):
        tool.install(repo.url)
    seen: list[str] = []
    with pytest.raises(InstallBlocked):
        tool.install(repo.url, confirm=lambda report: seen.append(report.verdict) is not None)
    assert seen == ["caution"]
    assert tool.install(repo.url, confirm=lambda report: True).report.verdict == "caution"
    assert tool.install(repo.url, force=True).replaced


def test_crash_between_staging_and_rename(
    gscope: Scope, repo: PluginRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo.commit(plugin_files())

    def crash(self: Installer, tree: Path, target: Path) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(Installer, "_publish", crash)
    with pytest.raises(KeyboardInterrupt):
        installer(gscope).install(repo.url)
    plugins = gscope.home / "plugins"
    assert not (plugins / "hello").exists() and Installer(gscope).records() == {}
    # A process that died mid-way leaves only dot-folders; the next install sweeps them.
    (plugins / ".staging-leftover").mkdir()
    monkeypatch.undo()
    installer(gscope).install(repo.url)
    assert (plugins / "hello" / "plugin.yaml").is_file()
    assert sorted(p.name for p in plugins.iterdir() if p.name.startswith(".s")) == []


def test_subdirectory_install_publishes_only_that_folder(gscope: Scope, repo: PluginRepo) -> None:
    files = {f"plugins/hello/{k}": v for k, v in plugin_files().items()}
    files["other/big.txt"] = "unrelated\n"
    repo.commit(files)
    result = installer(gscope).install(f"{repo.url}#plugins/hello")
    assert sorted(p.name for p in result.target.iterdir()) == ["__init__.py", "plugin.yaml"]
    assert Installer(gscope).record("hello")["subdir"] == "plugins/hello"  # type: ignore[index]


def test_local_folder_install(gscope: Scope, tmp_path: Path) -> None:
    folder = tmp_path / "mine"
    folder.mkdir()
    for rel, text in plugin_files("mine").items():
        (folder / rel).write_text(text, encoding="utf-8")
    (folder / ".git").mkdir()
    tool = installer(gscope)
    result = tool.install(str(folder))
    assert result.name == "mine" and not (result.target / ".git").exists()
    with pytest.raises(InstallError, match="reinstall"):
        tool.update("mine")


# -- update -------------------------------------------------------------------------------------


def test_update_reapplies_local_edits(gscope: Scope, repo: PluginRepo) -> None:
    repo.commit(plugin_files())
    tool = installer(gscope)
    target = tool.install(repo.url).target
    init = target / "__init__.py"
    init.write_text(init.read_text(encoding="utf-8") + "# my local tweak\n", encoding="utf-8")
    new = repo.commit({"README.md": "now documented\n"})
    result = tool.update("hello")
    assert result.changed and result.new == new and not result.blocked
    assert "# my local tweak" in init.read_text(encoding="utf-8")
    assert (target / "README.md").is_file()
    assert "re-applied" in result.note
    assert tool.record("hello")["revision"] == new  # type: ignore[index]
    again = tool.update("hello")
    assert not again.changed


def test_dangerous_update_restores_and_disables(gscope: Scope, repo: PluginRepo) -> None:
    first = repo.commit(plugin_files())
    tool = installer(gscope)
    target = tool.install(repo.url).target
    repo.commit({"__init__.py": DANGEROUS_INIT})
    result = tool.update("hello", force=True)
    assert result.blocked and result.disable and not result.changed
    assert inst.Git(gscope).head(target) == first
    assert "rm -rf" not in (target / "__init__.py").read_text(encoding="utf-8")
    assert tool.record("hello")["revision"] == first  # type: ignore[index]


def test_update_url_change_needs_trust(gscope: Scope, repo: PluginRepo) -> None:
    repo.commit(plugin_files())
    tool = installer(gscope)
    tool.install(repo.url)
    repo.commit(plugin_files(update_url="https://updates.example.com/hello"))
    stopped = tool.update("hello")
    assert "update_url changed" in stopped.blocked and not stopped.changed
    trusted = tool.update("hello", trust_update_url=True)
    assert trusted.changed
    assert tool.record("hello")["update_url"] == "https://updates.example.com/hello"  # type: ignore[index]


def test_force_reinstall_backs_up_edits(gscope: Scope, repo: PluginRepo) -> None:
    first = repo.commit(plugin_files())
    tool = installer(gscope)
    target = tool.install(repo.url).target
    (target / "__init__.py").write_text("# edited by hand\n", encoding="utf-8")
    result = tool.install(repo.url, force=True)
    assert result.backup == gscope.home / "plugins-backup" / f"hello-{first[:7]}"
    assert result.backup is not None
    assert (result.backup / "__init__.py").read_text(encoding="utf-8") == "# edited by hand\n"
    assert "register" in (target / "__init__.py").read_text(encoding="utf-8")


def test_check_updates(gscope: Scope, repo: PluginRepo, tmp_path: Path) -> None:
    repo.commit(plugin_files())
    tool = installer(gscope)
    tool.install(repo.url)
    other = PluginRepo(tmp_path / "pinned", "pinned")
    pin = other.commit(plugin_files("pinned"))
    tool.install(other.url, ref=pin)
    assert [(s.name, s.available) for s in tool.check_updates()] == [
        ("hello", False),
        ("pinned", False),
    ]
    new = repo.commit({"x.txt": "x\n"})
    statuses = {s.name: s for s in tool.check_updates()}
    assert statuses["hello"].available and statuses["hello"].latest == new
    assert statuses["pinned"].note == "pinned"
    assert tool.last_check()["available"] == {"hello": new}


def test_maybe_check_updates_honours_the_interval(gscope: Scope, repo: PluginRepo) -> None:
    repo.commit(plugin_files())
    installer(gscope).install(repo.url)
    off = gscope.with_config(DictConfig({"plugins": {"auto_update_check_hours": 0}}))
    assert Installer(off).maybe_check_updates() is None
    tool = installer(gscope)
    assert tool.maybe_check_updates(now=1_000_000.0) == []  # checked, not applied
    assert tool.maybe_check_updates(now=1_000_000.0 + 3600) is None  # not due yet
    repo.commit({"x.txt": "x\n"})
    auto = gscope.with_config(DictConfig({"plugins": {"auto_apply": True}}))
    applied = Installer(auto).maybe_check_updates(now=2_000_000.0 + 10**6)
    assert applied is not None and [a.changed for a in applied] == [True]


# -- remove -------------------------------------------------------------------------------------


def test_remove_and_forget(gscope: Scope, repo: PluginRepo) -> None:
    repo.commit(plugin_files())
    tool = installer(gscope)
    tool.install(repo.url)
    assert tool.remove("hello")
    assert not (gscope.home / "plugins" / "hello").exists() and tool.records() == {}
    assert not tool.remove("hello")
    tree: dict[str, Any] = {
        "plugins": {
            "enabled": ["hello", "other"],
            "disabled": ["hello"],
            "entries": {"hello": {"granted_capabilities": []}, "other": {}},
        },
        "memory": {"provider": "hello"},
    }
    changed = inst.forget_plugin(tree, "hello")
    assert tree["plugins"] == {"enabled": ["other"], "disabled": [], "entries": {"other": {}}}
    assert tree["memory"]["provider"] == ""
    assert "memory.provider" in changed


def test_metadata_holds_no_secrets_or_grants(gscope: Scope, repo: PluginRepo) -> None:
    repo.commit(plugin_files())
    installer(gscope).install(repo.url)
    data = json.loads((gscope.home / "plugins" / inst.METADATA_FILE).read_text(encoding="utf-8"))
    assert set(data["hello"]) == {
        "source",
        "kind",
        "identifier",
        "subdir",
        "revision",
        "pinned",
        "installed_at",
        "update_url",
        "scan",
    }
    assert os.stat(gscope.home / "plugins" / inst.METADATA_FILE).st_mode & 0o077 == 0
