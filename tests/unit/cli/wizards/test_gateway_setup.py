"""P16-31: `indus gateway setup` (scripted prompts, the real gateway registry rows with a fake
`check_fn` and a fake adapter, the profile's file secret store)."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner

from indus.cli import app as app_mod
from indus.cli.args import CliState
from indus.cli.prompts import Interactive
from indus.cli.wizards import gateway_setup as wizard
from indus.cli.wizards.gateway_setup import (
    CANCELLED,
    STEPS,
    GatewaySetupDeps,
    LiveGateway,
    run_gateway_setup,
)
from indus.foundation import config
from indus.foundation.config import loader
from indus.foundation.envload import env_file_path
from indus.foundation.scope import Scope
from indus.foundation.secrets import SecretStore, open_secret_store
from indus.gateway.adapters.base import Adapter, AdapterConnectError, SendResult
from indus.gateway.config import AdapterConfig
from indus.gateway.registry import BUILTIN_NAMES, PlatformRegistry, builtin_entries
from tests.fakes.prompts import CANCEL, ScriptedDriver

BOT_NAME = "indus_test_bot"


@dataclass
class World:
    """What the fake platforms saw: which adapters were opened, and how to make them fail."""

    opened: list[tuple[str, bool]] = field(default_factory=list[tuple[str, bool]])
    closed: list[str] = field(default_factory=list[str])
    ready: dict[str, bool] = field(default_factory=dict[str, bool])
    probed: list[str] = field(default_factory=list[str])
    refuse: dict[str, str] = field(default_factory=dict[str, str])


class FakeAdapter(Adapter):
    platform = "fake"

    def __init__(self, config_: AdapterConfig, world: World) -> None:
        super().__init__(clock=config_.clock, settings=config_.settings)
        self.config = config_
        self.world = world
        self.me = SimpleNamespace(username=BOT_NAME) if config_.name == "telegram" else None

    async def _open(self, is_reconnect: bool) -> None:
        self.world.opened.append((self.config.name, self.config.send_only))
        problem = self.world.refuse.get(self.config.name)
        if problem is not None:
            raise AdapterConnectError(problem, retryable=False)

    async def _close(self) -> None:
        self.world.closed.append(self.config.name)

    async def _send(
        self, chat_id: str, text: str, reply_to: str | None, meta: dict[str, Any]
    ) -> SendResult:
        raise AssertionError("the setup check never sends")


def fake_registry(world: World) -> PlatformRegistry:
    def probe(name: str) -> Callable[[], bool]:
        def check() -> bool:
            world.probed.append(name)
            return world.ready.get(name, True)

        return check

    def factory(config_: AdapterConfig) -> FakeAdapter:
        return FakeAdapter(config_, world)

    entries = [replace(e, check_fn=probe(e.name), factory=factory) for e in builtin_entries()]
    return PlatformRegistry(builtins=entries)


class RecordingDriver(ScriptedDriver):
    """Also keeps the rows of every choice (the menu's platform states)."""

    rows: list[list[str]]

    def __init__(self, answers: list[Any]) -> None:
        super().__init__(answers)
        self.rows = []

    def choose(self, question: str, rows: Sequence[str], default: int, *, allow_back: bool) -> int:
        self.rows.append(list(rows))
        return super().choose(question, rows, default, allow_back=allow_back)


@dataclass
class Run:
    code: int
    lines: list[str]
    driver: RecordingDriver

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


@pytest.fixture
def world() -> World:
    return World()


@pytest.fixture
def clean(scope: Scope) -> Iterator[Scope]:
    loader.reset()
    yield scope
    loader.reset()


def store_of(scope: Scope) -> SecretStore:
    store = scope.secrets
    assert isinstance(store, SecretStore)
    return store


def setup(scope: Scope, world: World, platform: str | None, *answers: Any, tty: bool = True) -> Run:
    lines: list[str] = []
    driver = RecordingDriver(list(answers))
    io = Interactive(tty=tty, driver=driver, say=lines.append)
    state = CliState(scope=scope, stdin_isatty=lambda: tty, stdout_isatty=lambda: tty)
    deps = GatewaySetupDeps(io=io, gateway=LiveGateway(fake_registry(world)), check_timeout=5)
    code = run_gateway_setup(state, platform, deps=deps)
    return Run(code, lines, driver)


def fake_value(name: str) -> str:
    """A value for an env name, built at run time (never a credential-shaped literal)."""
    return "v" + "-" + name.lower().replace("_", "-") + "-" + "x7"


def answers_for(name: str, *, access: str = "Ignore them", check: str = "y") -> list[Any]:
    """The script for one platform: every value, then the access, home and check questions."""
    steps = STEPS[name]
    out: list[Any] = []
    for ask in steps.asks:
        out.append(fake_value(ask.name) if ask.required else "")
    if steps.access:
        out += ["alice, <@bob>", access]
    if steps.home:
        out.append("")
    out.append(check)
    return out


def raw(scope: Scope) -> dict[str, Any]:
    return config.load(scope).user_raw()


def platform_raw(scope: Scope, name: str) -> dict[str, Any]:
    return raw(scope).get("platforms", {}).get(name, {})


SET_UP = [n for n in BUILTIN_NAMES if not STEPS[n].elsewhere]


@pytest.mark.parametrize("name", SET_UP)
def test_every_platform_runs_with_a_fake_check(clean: Scope, world: World, name: str) -> None:
    run = setup(clean, world, name, *answers_for(name))
    assert run.code == 0, run.text
    steps = STEPS[name]
    store = store_of(clean)
    secrets = [a for a in steps.asks if a.kind != "setting" and a.required]
    for ask in secrets:
        assert store.get(ask.name) == fake_value(ask.name)
    # secrets were typed masked, and are nowhere but the store
    masked = [a for a in steps.asks if a.kind == "secret"]
    assert len(run.driver.secrets_asked) == len(masked)
    path = config.config_path(clean)
    on_disk = path.read_text(encoding="utf-8") if path.exists() else ""
    for ask in (a for a in steps.asks if a.kind == "secret" and a.required):
        value = fake_value(ask.name)
        assert value not in on_disk
        assert value not in run.text
    assert not env_file_path(clean.home).exists()
    # settings went to config.yaml under platforms.<name>
    section = platform_raw(clean, name)
    for ask in (a for a in steps.asks if a.kind == "setting" and a.required):
        assert section[ask.name] == fake_value(ask.name)
    # a platform shipped disabled (Photon) is switched on by the wizard, not by its credentials
    assert section.get("enabled") is (True if steps.enable else None)
    if steps.access:
        assert section["extra"]["allow_from"] == ["alice", "bob"]
        assert section["extra"]["unauthorized_dm_behavior"] == "ignore"
        assert f"Unknown senders on {world_label(name)}: ignore" in run.text
    # the dependency probe ran and the adapter was opened send-only, then closed
    assert name in world.probed
    assert world.opened == [(name, True)] and world.closed == [name]
    assert "answered" in run.text


def world_label(name: str) -> str:
    entry = PlatformRegistry().get(name)
    assert entry is not None
    return entry.label


@pytest.mark.parametrize("name", ["api_server", "webhook", "whatsapp"])
def test_http_surfaces_point_elsewhere(clean: Scope, world: World, name: str) -> None:
    run = setup(clean, world, name)
    assert run.code == 0
    assert STEPS[name].elsewhere in run.text
    assert run.driver.asked == []


def test_menu_runs_until_done(clean: Scope, world: World) -> None:
    run = setup(clean, world, None, "Telegram (", *answers_for("telegram"), "Done")
    assert run.code == 0, run.text
    first, second = run.driver.rows[0], run.driver.rows[-1]
    assert "Telegram (not set up)" in first and first[-1] == "Done"
    assert len(first) == len(BUILTIN_NAMES) + 1
    assert "Telegram (on)" in second
    assert store_of(clean).get("TELEGRAM_BOT_TOKEN") == fake_value("TELEGRAM_BOT_TOKEN")


def test_an_existing_secret_is_kept_unless_replaced(clean: Scope, world: World) -> None:
    store = store_of(clean)
    first = "old" + "-token-" + "1"
    store.set("TELEGRAM_BOT_TOKEN", first)
    run = setup(clean, world, "telegram", "n", "", "Let the gateway", "", "n")
    assert run.code == 0, run.text
    assert store.get("TELEGRAM_BOT_TOKEN") == first
    assert run.driver.secrets_asked == []
    newer = "new" + "-token-" + "2"
    run = setup(clean, world, "telegram", "y", newer, "", "Let the gateway", "", "n")
    assert store.get("TELEGRAM_BOT_TOKEN") == newer
    assert newer not in run.text and first not in run.text


def test_a_missing_required_value_saves_nothing(clean: Scope, world: World) -> None:
    run = setup(clean, world, "matrix", "", fake_value("MATRIX_ACCESS_TOKEN"), "")
    assert run.code == 0
    assert "still needs platforms.matrix.homeserver" in run.text
    assert store_of(clean).get("MATRIX_ACCESS_TOKEN") is None
    assert platform_raw(clean, "matrix") == {}
    assert world.opened == []


def _other_profile(scope: Scope, values: dict[str, str]) -> None:
    """The default profile (the clone's source) holding ``values`` in its own file store."""
    root = scope.root
    config.save(
        replace(scope, home=root, profile_id="default"), {"security": {"secret_store": "file"}}
    )
    store = open_secret_store(root, "default", mode="file")
    for name, value in values.items():
        store.set(name, value)


def test_a_bot_token_of_another_profile_is_refused(clean: Scope, world: World) -> None:
    token = fake_value("TELEGRAM_BOT_TOKEN")
    _other_profile(clean, {"TELEGRAM_BOT_TOKEN": token})
    run = setup(clean, world, "telegram", token)
    assert run.code == 0
    assert "Refused: profile 'default' already uses this Telegram bot" in run.text
    assert token not in run.text
    assert store_of(clean).get("TELEGRAM_BOT_TOKEN") is None
    assert world.opened == []
    # a bot of its own is fine
    own = "own" + "-token-" + "3"
    run = setup(clean, world, "telegram", own, *answers_for("telegram")[1:])
    assert "Refused" not in run.text
    assert store_of(clean).get("TELEGRAM_BOT_TOKEN") == own


def test_the_lock_identity_decides_not_each_value(clean: Scope, world: World) -> None:
    # Teams locks on client id + tenant: the same tenant with another app is another bot.
    _other_profile(
        clean,
        {
            "TEAMS_CLIENT_ID": "other" + "-app",
            "TEAMS_CLIENT_SECRET": "s" + "1",
            "TEAMS_TENANT_ID": fake_value("TEAMS_TENANT_ID"),
        },
    )
    run = setup(clean, world, "teams", *answers_for("teams", check="n"))
    assert "Refused" not in run.text
    assert store_of(clean).get("TEAMS_TENANT_ID") == fake_value("TEAMS_TENANT_ID")
    # IRC locks on server + nick, which are not secrets: the same pair is refused
    _other_profile(clean, {"IRC_SERVER": "irc.example.org", "IRC_NICKNAME": "indusbot"})
    run = setup(clean, world, "irc", "irc.example.org", "indusbot", "", "")
    assert "Refused: profile 'default' already uses this IRC bot" in run.text
    assert store_of(clean).get("IRC_SERVER") is None


def test_a_missing_library_prints_the_hint_and_skips_the_check(clean: Scope, world: World) -> None:
    world.ready["discord"] = False
    run = setup(clean, world, "discord", *answers_for("discord")[:-1])
    assert run.code == 0, run.text
    assert "library is not installed: pip install 'indus-agent[discord]'" in run.text
    assert "Connection check skipped" in run.text
    assert store_of(clean).get("DISCORD_BOT_TOKEN") == fake_value("DISCORD_BOT_TOKEN")
    assert world.opened == []


def test_slack_prints_a_manifest(clean: Scope, world: World) -> None:
    run = setup(clean, world, "slack", *answers_for("slack"))
    assert '"socket_mode_enabled": true' in run.text
    assert '"chat:write"' in run.text and '"message.im"' in run.text


def test_telegram_prints_the_bot_link(clean: Scope, world: World) -> None:
    run = setup(clean, world, "telegram", *answers_for("telegram"))
    assert f"https://t.me/{BOT_NAME}" in run.text


def test_a_failed_check_is_reported_with_secrets_masked(clean: Scope, world: World) -> None:
    secret = fake_value("WHATSAPP_CLOUD_APP_SECRET")
    world.refuse["whatsapp_cloud"] = f"the app secret {secret} was refused"
    run = setup(clean, world, "whatsapp_cloud", *answers_for("whatsapp_cloud"))
    assert run.code == 0
    assert "Connection check failed: the app secret **** was refused" in run.text
    assert secret not in run.text
    assert store_of(clean).get("WHATSAPP_CLOUD_APP_SECRET") == secret


def test_access_modes_are_explained(clean: Scope, world: World) -> None:
    run = setup(clean, world, "telegram", *answers_for("telegram", access="Send them a pairing"))
    assert (
        "Unknown senders on Telegram: pair (set by platforms.telegram.extra."
        "unauthorized_dm_behavior)" in run.text
    )
    run = setup(clean, world, "email", *answers_for("email", access="Let the gateway"))
    assert "Unknown senders on Email: ignore (email never answers unknown senders" in run.text
    # ntfy is a broadcast surface: no pairing row, and the note says why
    run = setup(clean, world, "ntfy", *answers_for("ntfy"))
    access_rows = run.driver.rows[-1]
    assert not any("pairing code" in row for row in access_rows)
    assert "Pairing codes and sign-in links are never sent on ntfy" in run.text


def test_the_gateway_decides_when_nothing_is_chosen(clean: Scope, world: World) -> None:
    script = [fake_value("DISCORD_BOT_TOKEN"), "", "Let the gateway", "", "n"]
    run = setup(clean, world, "discord", *script)
    assert "Unknown senders on Discord: pair (no allowlist is configured" in run.text
    assert "extra" not in platform_raw(clean, "discord")


def test_the_home_channel_follows_the_target_rule(clean: Scope, world: World) -> None:
    script = answers_for("telegram")
    home_at = script.index("")  # the home question's blank answer
    script[home_at : home_at + 1] = ["#general", "abc", "-1001234567890:5"]
    run = setup(clean, world, "telegram", *script)
    assert run.code == 0, run.text
    assert "names are looked up only when sending" in run.text
    assert "is not a telegram target" in run.text
    assert platform_raw(clean, "telegram")["home_channel"] == {
        "chat_id": "-1001234567890",
        "thread_id": "5",
    }


def test_the_environment_value_is_never_copied(clean: Scope, world: World) -> None:
    from indus.foundation.scope import EnvView

    token = fake_value("DISCORD_BOT_TOKEN")
    scope = clean.with_env(EnvView({**dict(clean.env.items()), "DISCORD_BOT_TOKEN": token}))
    run = setup(scope, world, "discord", "", "", "Let the gateway", "", "y")
    assert "DISCORD_BOT_TOKEN is set in the environment" in run.text
    assert store_of(scope).get("DISCORD_BOT_TOKEN") is None
    assert world.opened == [("discord", True)]


def test_cancel_exits_1(clean: Scope, world: World) -> None:
    run = setup(clean, world, "telegram", CANCEL)
    assert run.code == 1
    assert CANCELLED in run.text


def test_unknown_platform_exits_2(
    clean: Scope, world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    run = setup(clean, world, "carrier-pigeon")
    assert run.code == 2
    assert "no platform called 'carrier-pigeon'" in capsys.readouterr().err


def test_without_a_terminal_the_keys_are_printed(
    clean: Scope, world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    run = setup(clean, world, "matrix", tty=False)
    assert run.code == 2
    out = capsys.readouterr().out
    assert "MATRIX_ACCESS_TOKEN" in out and "platforms.matrix.homeserver" in out
    assert "platforms.matrix.extra.allow_from (optional)" in out
    assert "telegram:" not in out
    assert run.driver.asked == []
    run = setup(clean, world, None, tty=False)
    out = capsys.readouterr().out
    assert run.code == 2
    for name in BUILTIN_NAMES:
        assert f"{name}:" in out


def test_the_command_is_wired_under_gateway(clean: Scope) -> None:
    state = CliState(scope=clean, stdin_isatty=lambda: False, stdout_isatty=lambda: False)
    runner = CliRunner()
    with runner.isolation() as (out, _err, _mixed):
        code = app_mod.dispatch(["gateway", "setup", "telegram"], state)
    assert code == 2
    assert "TELEGRAM_BOT_TOKEN" in out.getvalue().decode()
    with runner.isolation() as (out, _err, _mixed):
        code = app_mod.dispatch(["gateway", "--help"], state)
    assert code == 0 and "setup" in out.getvalue().decode()


def test_the_slack_manifest_is_valid_json() -> None:
    import json

    data = json.loads(wizard.slack_manifest())
    assert data["settings"]["socket_mode_enabled"] is True
    assert "im:write" in data["oauth_config"]["scopes"]["bot"]


def test_bot_identity_mirrors_the_lock_key() -> None:
    values = {"A": "1", "B": "2", "C": ""}
    assert wizard.bot_identity(lambda n: values.get(n, ""), ("B", "A"), ("A",)) == "2\n1"
    assert wizard.bot_identity(lambda n: values.get(n, ""), (), ("B", "A")) == "1\n2"
    assert wizard.bot_identity(lambda n: values.get(n, ""), ("C",), ("A", "C")) == ""
    assert wizard.bot_identity(lambda n: values.get(n, ""), ("C",), ("A",)) == "1"


def test_the_other_profiles_reader_skips_this_profile(clean: Scope) -> None:
    _other_profile(clean, {"TELEGRAM_BOT_TOKEN": "t" + "1"})
    store_of(clean).set("TELEGRAM_BOT_TOKEN", "t" + "2")
    found = dict(wizard.OtherProfiles().values(clean, ["TELEGRAM_BOT_TOKEN"]))
    assert found == {"default": {"TELEGRAM_BOT_TOKEN": "t1"}}
    # a .env value of another profile counts too
    (clean.root / ".env").write_text("SLACK_BOT_TOKEN=s9\n", encoding="utf-8")
    found = dict(wizard.OtherProfiles().values(clean, ["SLACK_BOT_TOKEN"]))
    assert found["default"]["SLACK_BOT_TOKEN"] == "s9"


def test_every_builtin_platform_has_steps() -> None:
    assert set(STEPS) == set(BUILTIN_NAMES)
    for name, steps in STEPS.items():
        if steps.elsewhere:
            continue
        assert steps.intro and steps.asks, name


def test_a_plugin_platform_asks_its_registry_env_names(clean: Scope, world: World) -> None:
    info = wizard.PlatformInfo(name="pigeon", label="Pigeon", required_env=("PIGEON_KEY",))
    steps = wizard._steps_for(info)  # pyright: ignore[reportPrivateUsage]
    assert [(a.name, a.kind) for a in steps.asks] == [("PIGEON_KEY", "secret")]


def test_telegram_offers_the_first_allowed_person_as_home(clean: Scope, world: World) -> None:
    script = [fake_value("TELEGRAM_BOT_TOKEN"), "123456789, 42", "Ignore them", "y", "n"]
    run = setup(clean, world, "telegram", *script)
    assert run.code == 0, run.text
    assert any("Use 123456789 (the first allowed person)" in q for q in run.driver.asked)
    assert platform_raw(clean, "telegram")["home_channel"] == {"chat_id": "123456789"}


def test_the_suggested_home_can_be_declined(clean: Scope, world: World) -> None:
    script = [fake_value("TELEGRAM_BOT_TOKEN"), "123456789", "Ignore them", "n", "", "n"]
    run = setup(clean, world, "telegram", *script)
    assert run.code == 0, run.text
    assert "home_channel" not in platform_raw(clean, "telegram")


def test_a_platform_left_on_suggests_the_waiting_jobs_once(clean: Scope, world: World) -> None:
    """Wave 45 (D-16.981): the starter briefing waits for a chat platform; it is suggested, never
    scheduled, and a second platform does not suggest it again."""
    from indus.cron.suggestions import SuggestionStore

    run = setup(clean, world, "telegram", *answers_for("telegram"))
    assert run.code == 0
    assert "Suggested job: Morning briefing" in run.text
    pending = SuggestionStore(clean).pending()
    assert [(s.dedup_key, s.source) for s in pending] == [
        ("blueprint:morning-brief", "integration")
    ]
    again = setup(clean, world, "slack", *answers_for("slack"))
    assert "Suggested job:" not in again.text
    assert len(SuggestionStore(clean).pending()) == 1


def test_a_platform_that_stays_off_suggests_nothing(clean: Scope, world: World) -> None:
    from indus.cron.suggestions import store_path

    run = setup(clean, world, "telegram", "")  # the token left empty: nothing saved
    assert "Suggested job:" not in run.text
    assert not store_path(clean).exists()
