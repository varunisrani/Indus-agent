"""P07-16: `indus profile` (R04.25 marker-less dirs and tombstones, R04.26 clean clones)."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

import pytest

from indus.cli import entry, prompts
from indus.cli.commands import profile as profile_cmd
from indus.foundation import secrets, yamlio
from indus.foundation.envfile import read_env_file
from indus.foundation.profiles import is_live, is_tombstoned, read_active
from indus.foundation.scope import Scope
from indus.foundation.secrets import KeychainBackend, SecretStore, open_secret_store
from tests.fakes.clock import FakeClock
from tests.fakes.prompts import scripted
from tests.unit.cli.commands.cmdrun import root_scope, run_cli

BOT_TOKEN = "123456" + ":" + "fake-bot-token-value"
MODEL_KEY = "test" + "-key-" + "0000abcd1234wxyz"


class FakeKeyring:
    priority = 1

    def __init__(self) -> None:
        self.data: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str, /) -> str | None:
        return self.data.get((service, username))

    def set_password(self, service: str, username: str, password: str, /) -> None:
        self.data[(service, username)] = password

    def delete_password(self, service: str, username: str, /) -> None:
        del self.data[(service, username)]


@pytest.fixture
def root(isolated_home: Path, fake_clock: FakeClock) -> Scope:
    return root_scope(isolated_home, fake_clock)


def _seed_source(home: Path) -> None:
    (home / ".env").write_text(
        f"TELEGRAM_BOT_TOKEN={BOT_TOKEN}\nTELEGRAM_ALLOWED_USERS=42\nOPENAI_API_KEY={MODEL_KEY}\n",
        encoding="utf-8",
    )
    (home / "config.yaml").write_text(
        "# mine\nmodel:\n  default: test-model\n  provider: openrouter\n"
        "telegram:\n  home_channel: '42'\ngateway:\n  discord:\n    x: 1\n  other: 2\n",
        encoding="utf-8",
    )
    (home / "PERSONA.md").write_text("I am the source.\n", encoding="utf-8")
    (home / "memories").mkdir(exist_ok=True)
    (home / "memories" / "MEMORY.md").write_text("remember this\n", encoding="utf-8")
    (home / "skills" / "greet").mkdir(parents=True, exist_ok=True)
    (home / "skills" / "greet" / "SKILL.md").write_text("say hi\n", encoding="utf-8")


def _raw(home: Path) -> dict[str, Any]:
    loaded = yamlio.load_safe((home / "config.yaml").read_bytes())
    return loaded if isinstance(loaded, dict) else {}


def _store(home: Path, pid: str) -> SecretStore:
    return open_secret_store(home, pid, mode="file")


# -- create / clone -------------------------------------------------------------------------------


def test_clone_strips_channels(capsys: pytest.CaptureFixture[str], root: Scope) -> None:
    _seed_source(root.home)
    source_store = _store(root.home, "default")
    source_store.set("TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    source_store.set("OPENAI_API_KEY", MODEL_KEY)
    env_before = (root.home / ".env").read_bytes()

    ran = run_cli(capsys, root, "profile", "create", "work", "--clone", "--no-alias")
    assert ran.code == 0, ran.err
    work = root.home / "profiles" / "work"
    assert is_live(root.home, "work")
    assert read_env_file(work / ".env") == {"OPENAI_API_KEY": MODEL_KEY}
    settings = _raw(work)
    assert "telegram" not in settings
    assert settings["gateway"] == {"other": 2}
    assert settings["model"]["default"] == "test-model"
    assert (work / "PERSONA.md").read_text() == "I am the source.\n"
    assert (work / "memories" / "MEMORY.md").is_file()
    assert (work / "skills" / "greet" / "SKILL.md").is_file()
    assert _store(work, "work").names() == ["OPENAI_API_KEY"]
    # nothing in the new profile still holds the bot token (backups included)
    for path in work.rglob("*"):
        if path.is_file():
            assert BOT_TOKEN.encode() not in path.read_bytes(), path
    assert "TELEGRAM_BOT_TOKEN" in ran.out
    assert (root.home / ".env").read_bytes() == env_before
    assert stat.S_IMODE(work.stat().st_mode) == 0o700


def test_clone_leaves_pool_secrets_behind(capsys: pytest.CaptureFixture[str], root: Scope) -> None:
    import json

    from indus.providers.auth.pool_model import secret_name
    from indus.providers.auth.store import AUTH_FILE

    _seed_source(root.home)
    source_store = _store(root.home, "default")
    source_store.set("OPENAI_API_KEY", MODEL_KEY)
    source_store.set(secret_name("openrouter", "r1"), MODEL_KEY + "-pool")
    source_store.set(secret_name("acme", "0a1b2c3d"), MODEL_KEY + "-orphan")
    doc = {"version": 1, "pool": {"openrouter": [{"id": "r1", "label": "mine"}]}}
    (root.home / AUTH_FILE).write_text(json.dumps(doc), encoding="utf-8")

    ran = run_cli(capsys, root, "profile", "create", "work", "--clone-all", "--no-alias")
    assert ran.code == 0, ran.err
    work = root.home / "profiles" / "work"
    assert _store(work, "work").names() == ["OPENAI_API_KEY"]
    assert not (work / AUTH_FILE).exists()


def test_clone_channels_keeps_them(capsys: pytest.CaptureFixture[str], root: Scope) -> None:
    _seed_source(root.home)
    ran = run_cli(
        capsys, root, "profile", "create", "work", "--clone", "--clone-channels", "--no-alias"
    )
    assert ran.code == 0, ran.err
    work = root.home / "profiles" / "work"
    assert read_env_file(work / ".env")["TELEGRAM_BOT_TOKEN"] == BOT_TOKEN
    assert "telegram" in _raw(work)


def test_clone_through_symlinked_env(
    capsys: pytest.CaptureFixture[str], root: Scope, tmp_path: Path
) -> None:
    _seed_source(root.home)
    shared = tmp_path / "shared.env"
    shared.write_bytes((root.home / ".env").read_bytes())
    (root.home / ".env").unlink()
    (root.home / ".env").symlink_to(shared)
    before = shared.read_bytes()
    for name, flag in (("one", "--clone"), ("two", "--clone-all")):
        ran = run_cli(capsys, root, "profile", "create", name, flag, "--no-alias")
        assert ran.code == 0, ran.err
        env_file = root.home / "profiles" / name / ".env"
        assert not env_file.is_symlink()
        assert "TELEGRAM_BOT_TOKEN" not in read_env_file(env_file)
    assert shared.read_bytes() == before


def test_clone_all_skips_history(capsys: pytest.CaptureFixture[str], root: Scope) -> None:
    _seed_source(root.home)
    home = root.home
    (home / "state.db").write_bytes(b"db")
    (home / "state.db-wal").write_bytes(b"wal")
    for folder in ("sessions", "logs", "cron"):
        (home / folder).mkdir(exist_ok=True)
    (home / "sessions" / "old.jsonl").write_text("x")
    (home / "logs" / "agent.log").write_text("log")
    (home / "cron" / "jobs.json").write_text("{}")
    (home / "notes").mkdir()
    (home / "notes" / "keep.txt").write_text("keep")
    (home / "pairing").mkdir()
    (home / "pairing" / "approved.json").write_text("{}")
    ran = run_cli(capsys, root, "profile", "create", "copy", "--clone-all", "--no-alias")
    assert ran.code == 0, ran.err
    copy = home / "profiles" / "copy"
    assert (copy / "notes" / "keep.txt").read_text() == "keep"
    assert (copy / "skills" / "greet" / "SKILL.md").is_file()
    for gone in (
        "state.db",
        "state.db-wal",
        "sessions",
        "logs",
        "cron",
        "pairing",
        "profiles",
        "backups",
    ):
        assert not (copy / gone).exists(), gone


def test_keychain_entries_copied_to_new_namespace(
    capsys: pytest.CaptureFixture[str], root: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    keyring = FakeKeyring()
    monkeypatch.setattr(secrets, "_system_keyring", lambda: keyring)
    secrets.reset_probe_cache()
    store = SecretStore(KeychainBackend(keyring, "default"), root.home / "secrets.index.json")
    store.set("OPENAI_API_KEY", MODEL_KEY)
    store.set("DISCORD_BOT_TOKEN", BOT_TOKEN)
    scope = root.with_secrets(store)
    ran = run_cli(capsys, scope, "profile", "create", "work", "--clone", "--no-alias")
    assert ran.code == 0, ran.err
    assert keyring.data[("indus", "work:OPENAI_API_KEY")] == MODEL_KEY
    assert ("indus", "work:DISCORD_BOT_TOKEN") not in keyring.data
    assert keyring.data[("indus", "default:DISCORD_BOT_TOKEN")] == BOT_TOKEN


def test_failed_clone_leaves_nothing(
    capsys: pytest.CaptureFixture[str], root: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_source(root.home)

    def broken(*args: object) -> list[str]:
        raise OSError("disk full")

    monkeypatch.setattr(profile_cmd, "_strip_settings", broken)
    ran = run_cli(capsys, root, "profile", "create", "work", "--clone", "--no-alias")
    assert ran.code == 1
    assert "disk full" in ran.err
    assert sorted(p.name for p in (root.home / "profiles").iterdir()) == []


def test_create_refusals(capsys: pytest.CaptureFixture[str], root: Scope) -> None:
    assert run_cli(capsys, root, "profile", "create", "default", "--no-alias").code == 1
    assert run_cli(capsys, root, "profile", "create", "Bad Name!", "--no-alias").code == 1
    assert run_cli(capsys, root, "profile", "create", "work", "--no-alias").code == 0
    ran = run_cli(capsys, root, "profile", "create", "work", "--no-alias")
    assert ran.code == 1
    assert "already exists" in ran.err
    ran = run_cli(capsys, root, "profile", "create", "x", "--clone-channels", "--no-alias")
    assert ran.code == 1


def test_marker_less_dir_untouched(capsys: pytest.CaptureFixture[str], root: Scope) -> None:
    ghost = root.home / "profiles" / "ghost"
    ghost.mkdir(parents=True)
    (ghost / "notes.txt").write_text("mine")
    ran = run_cli(capsys, root, "profile", "create", "ghost", "--no-alias")
    assert ran.code == 1
    assert "left untouched" in ran.err
    ran = run_cli(capsys, root, "profile", "delete", "ghost", "-y")
    assert ran.code == 1
    assert (ghost / "notes.txt").read_text() == "mine"
    assert not is_tombstoned(root.home, "ghost")


# -- delete ---------------------------------------------------------------------------------------


def test_delete_tombstones_and_profile_flag_refuses(
    capsys: pytest.CaptureFixture[str], root: Scope
) -> None:
    assert run_cli(capsys, root, "profile", "create", "work", "--no-alias").code == 0
    work = root.home / "profiles" / "work"
    _store(work, "work").set("OPENAI_API_KEY", MODEL_KEY)
    assert run_cli(capsys, root, "profile", "use", "work").code == 0

    ran = run_cli(capsys, root, "profile", "delete", "work")  # no terminal, no -y
    assert ran.code == 1
    assert "-y" in ran.err
    assert is_live(root.home, "work")

    ran = run_cli(capsys, root, "profile", "delete", "work", "-y")
    assert ran.code == 0, ran.err
    assert is_tombstoned(root.home, "work")
    assert not work.exists()
    assert read_active(root.home) == "default"

    env = {k: v for k, v in os.environ.items() if not k.startswith("INDUS_")}
    env["INDUS_HOME"] = str(root.home)
    code = entry.main(["-p", "work", "profile", "list"], env=env)
    assert code == 1
    assert "deleted" in capsys.readouterr().err


def test_delete_asks_on_a_terminal(
    capsys: pytest.CaptureFixture[str], root: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert run_cli(capsys, root, "profile", "create", "work", "--no-alias").code == 0
    answers = scripted("wrong", "work")
    monkeypatch.setattr(prompts, "io_factory", lambda _state, _guidance: answers)
    assert run_cli(capsys, root, "profile", "delete", "work", tty=True).code == 1
    assert is_live(root.home, "work")
    assert run_cli(capsys, root, "profile", "delete", "work", tty=True).code == 0
    assert not is_live(root.home, "work")


def test_default_cannot_be_deleted_or_renamed(
    capsys: pytest.CaptureFixture[str], root: Scope
) -> None:
    assert run_cli(capsys, root, "profile", "delete", "default", "-y").code == 1
    assert run_cli(capsys, root, "profile", "rename", "default", "main").code == 1


# -- list / use / show / describe / rename --------------------------------------------------------


def test_list_use_show_describe(capsys: pytest.CaptureFixture[str], root: Scope) -> None:
    ran = run_cli(
        capsys, root, "profile", "create", "work", "--no-alias", "--description", "Day job"
    )
    assert ran.code == 0, ran.err
    (root.home / "profiles" / "work" / "config.yaml").write_text("model: test-model\n")
    ran = run_cli(capsys, root, "profile")
    assert ran.code == 0
    lines = ran.out.splitlines()
    assert lines[0].startswith("*> default")
    assert "work" in lines[1] and "test-model" in lines[1]

    assert run_cli(capsys, root, "profile", "use", "work").code == 0
    assert read_active(root.home) == "work"
    assert run_cli(capsys, root, "profile", "use", "nope").code == 1
    assert run_cli(capsys, root, "profile", "use", "default").code == 0
    assert read_active(root.home) == "default"

    ran = run_cli(capsys, root, "profile", "show", "work")
    assert "Model:   test-model" in ran.out
    assert "About:   Day job" in ran.out

    assert run_cli(capsys, root, "profile", "describe", "work").out.strip() == "Day job"
    assert run_cli(capsys, root, "profile", "describe", "work", "--text", "Weekend").code == 0
    assert run_cli(capsys, root, "profile", "describe", "work").out.strip() == "Weekend"
    assert "no description" in run_cli(capsys, root, "profile", "describe").out


def test_rename_moves_everything(capsys: pytest.CaptureFixture[str], root: Scope) -> None:
    assert run_cli(capsys, root, "profile", "create", "work").code == 0
    assert run_cli(capsys, root, "profile", "use", "work").code == 0
    ran = run_cli(capsys, root, "profile", "rename", "work", "job")
    assert ran.code == 0, ran.err
    assert is_live(root.home, "job") and not is_live(root.home, "work")
    assert read_active(root.home) == "job"
    alias = profile_cmd.alias_dir(root.env) / "work"
    assert "-p job" in alias.read_text()
    assert run_cli(capsys, root, "profile", "rename", "job", "job").code == 1


# -- alias ----------------------------------------------------------------------------------------


def test_alias_script_golden() -> None:
    assert profile_cmd.alias_script("work", ["/opt/indus/bin/indus"]) == (
        '#!/bin/sh\n# indus profile alias\nexec /opt/indus/bin/indus -p work "$@"\n'
    )
    assert profile_cmd.alias_script("work", ["/my dir/python", "-m", "indus"]) == (
        "#!/bin/sh\n# indus profile alias\nexec '/my dir/python' -m indus -p work \"$@\"\n"
    )


def test_create_writes_alias(capsys: pytest.CaptureFixture[str], root: Scope) -> None:
    ran = run_cli(capsys, root, "profile", "create", "work")
    assert ran.code == 0, ran.err
    script = profile_cmd.alias_dir(root.env) / "work"
    assert script.is_file()
    assert script.stat().st_mode & stat.S_IXUSR
    assert script.read_text().endswith(' -p work "$@"\n')
    assert "is not on your PATH" in ran.err
    ran = run_cli(capsys, root, "profile", "alias", "work", "--remove")
    assert ran.code == 0
    assert not script.exists()
    assert run_cli(capsys, root, "profile", "alias", "work", "--name", "wk").code == 0
    assert "-p work" in (profile_cmd.alias_dir(root.env) / "wk").read_text()


def test_alias_collisions_refused(capsys: pytest.CaptureFixture[str], root: Scope) -> None:
    assert run_cli(capsys, root, "profile", "create", "work", "--no-alias").code == 0
    folder = profile_cmd.alias_dir(root.env)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "mine").write_text("#!/bin/sh\necho someone else's\n")
    for alias, reason in (
        ("chat", "is an `indus` command"),
        ("default", "reserved"),
        ("mine", "was not made by"),
        ("Bad/Name", "cannot be an alias"),
    ):
        ran = run_cli(capsys, root, "profile", "alias", "work", "--name", alias)
        assert ran.code == 1, alias
        assert reason in ran.err, alias
    assert (folder / "mine").read_text() == "#!/bin/sh\necho someone else's\n"
    ran = run_cli(capsys, root, "profile", "alias", "work", "--name", "mine", "--remove")
    assert ran.code == 1
    assert (folder / "mine").exists()
