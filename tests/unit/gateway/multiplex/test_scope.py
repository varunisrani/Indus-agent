"""P24-02: each served profile's scope, the fail-closed reader and activity binding."""

from __future__ import annotations

import asyncio
import logging
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.scope import Scope, current_scope, scope_or_none, use_scope
from indus.gateway.core.authz import Authorizer
from indus.gateway.core.events import Attachment, InboundEvent
from indus.gateway.multiplex.scope import (
    ACTIVITIES,
    SECONDARY_OS_NAMES,
    ProbeGate,
    ProfileScopeCache,
    ScopeBinder,
    ScopedReader,
    adopt_inbound_media,
    build_served_scope,
    scoped_reader_for,
    secondary_base_env,
)
from indus.gateway.registry import PlatformRegistry
from indus.learning.memory.store import FILE_NAMES, MemoryStore, Op
from tests.fakes.clock import FakeClock
from tests.unit.gateway.builders import src
from tests.unit.gateway.multiplex.world import launch_scope, make_profile

ALLOW = "INDUS_TELEGRAM_ALLOWED_USERS"
TOKEN = "TELEGRAM_BOT_TOKEN"
MARKER = "LAUNCH_ONLY_MARKER"


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def launch_token() -> str:
    return "launch-" + "t" * 12  # built at runtime: not a credential shape


@pytest.fixture
def root(isolated_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv(ALLOW, "42")
    monkeypatch.setenv(TOKEN, launch_token())
    monkeypatch.setenv(MARKER, "1")
    return isolated_home


@pytest.fixture
def launch(root: Path, fake_clock: FakeClock) -> Scope:
    return launch_scope(root, fake_clock)


@pytest.fixture
def home_b(root: Path) -> Path:
    return make_profile(root, "b")


@pytest.fixture
def secondary(launch: Scope, home_b: Path, fake_clock: FakeClock) -> Scope:
    return build_served_scope(launch, home_b, clock=fake_clock)


def dm(user: str) -> Any:
    return src("telegram", user, user_id=user)


# -- building --------------------------------------------------------------------------------------


def test_the_secondary_view_has_none_of_the_launch_env(
    launch: Scope, secondary: Scope, home_b: Path
) -> None:
    assert launch.env[TOKEN] == launch_token() and launch.env[MARKER] == "1"
    for name in (TOKEN, ALLOW, MARKER):
        assert name not in secondary.env
    assert not any(
        name.startswith("INDUS_") and name != "INDUS_HOME" for name in secondary.env.base
    )
    assert secondary.env["INDUS_HOME"] == str(home_b)
    if "PATH" in launch.env:
        assert secondary.env["PATH"] == launch.env["PATH"]
    assert (secondary.profile_id, secondary.routed, secondary.root) == ("b", True, launch.root)
    assert secondary.secrets is not None and secondary.secrets is not launch.secrets
    assert secondary.config is not None


def test_the_launch_home_gives_back_the_launch_scope(launch: Scope, root: Path) -> None:
    assert build_served_scope(launch, root) is launch


def test_secondary_base_env_keeps_only_os_names() -> None:
    env = {"PATH": "/bin", "LANG": "C", TOKEN: launch_token(), "HTTPS_PROXY": "http://p", "X": "1"}
    assert secondary_base_env(env) == {"PATH": "/bin", "LANG": "C"}
    assert "HTTPS_PROXY" not in SECONDARY_OS_NAMES


def test_building_a_secondary_never_touches_the_process_env(launch: Scope, root: Path) -> None:
    home = make_profile(root, "c", env_lines={"INDUS_SLACK_ALLOWED_USERS": "9"})
    (home / "config.yaml").write_text(
        "security:\n  secret_store: file\nplatforms:\n  telegram:\n    extra:\n"
        "      allow_from: ['7']\nterminal:\n  backend: local\n",
        encoding="utf-8",
    )
    before = dict(os.environ)
    launch_before = launch.env.to_dict()
    scope = build_served_scope(launch, home)
    assert dict(os.environ) == before
    assert launch.env.to_dict() == launch_before
    assert scope.env["INDUS_SLACK_ALLOWED_USERS"] == "9"
    assert scope.terminal_policy.get("backend") == "local"


def test_the_scope_cache_builds_once_and_forgets_on_drop(launch: Scope, home_b: Path) -> None:
    cache = ProfileScopeCache(launch)
    first = cache.get("b")
    assert cache.get("b") is first and cache.get("default") is launch
    assert first.home == home_b
    cache.drop("b")
    cache.drop("default")
    assert cache.profiles() == ["default"]
    assert cache.get("b") is not first


# -- reading ---------------------------------------------------------------------------------------


def test_a_secondary_without_its_own_allowlist_denies(launch: Scope, secondary: Scope) -> None:
    registry = PlatformRegistry()
    on_launch = Authorizer.for_scope(launch, registry)
    on_b = Authorizer.for_scope(secondary, registry)
    assert on_launch.profile == "default" and on_b.profile == "b"
    assert on_launch.check(dm("42")).granted
    refused = on_b.check(dm("42"))
    assert not refused.granted and refused.step == "default_deny"
    assert on_b.configured_allowlists("telegram") == []

    assert secondary.secrets is not None
    secondary.secrets.set(ALLOW, "77")  # type: ignore[attr-defined]
    assert on_b.check(dm("77")).granted
    assert not on_b.check(dm("42")).granted
    assert not on_launch.check(dm("77")).granted


def test_a_secondary_reads_its_own_env_file(launch: Scope, root: Path) -> None:
    home = make_profile(root, "c", env_lines={ALLOW: "55"})
    scope = build_served_scope(launch, home)
    authz = Authorizer.for_scope(scope, PlatformRegistry())
    assert authz.check(dm("55")).granted
    assert not authz.check(dm("42")).granted
    assert ScopedReader(scope).get(TOKEN) is None


def test_a_routed_scope_built_the_wrong_way_still_fails_closed(launch: Scope) -> None:
    wrong = Scope(
        profile_id="b",
        home=launch.home / "profiles" / "b",
        root=launch.root,
        env=launch.env.with_overlay({"OWN_VALUE": "yes"}),
        clock=launch.clock,
        routed=True,
    )
    reader = ScopedReader(wrong)
    assert reader.get(ALLOW) is None and reader.get(TOKEN, "fallback") == "fallback"
    assert reader.get("OWN_VALUE") == "yes"


def test_the_launch_profile_reads_its_env_after_its_secrets(launch: Scope) -> None:
    reader = ScopedReader(launch)
    assert reader.get(ALLOW) == "42"
    assert launch.secrets is not None
    launch.secrets.set(ALLOW, " 43 ")  # type: ignore[attr-defined]
    assert reader.get(ALLOW) == "43"
    assert reader.get("NOT_SET_ANYWHERE", "d") == "d"
    assert reader.get("") is None
    assert scoped_reader_for(launch)(ALLOW) == "43"


def test_flags_read_known_words_only(launch: Scope) -> None:
    scope = launch.with_env(launch.env.with_overlay({"A": "yes", "B": "off", "C": "maybe"}))
    reader = ScopedReader(scope)
    assert reader.flag("A") is True and reader.flag("B") is False
    assert reader.flag("C", default=True) is True and reader.flag("MISSING") is False


def test_an_unreadable_secret_store_falls_to_the_profiles_own_view(
    secondary: Scope, caplog: pytest.LogCaptureFixture
) -> None:
    class Broken:
        generation = 0

        def get(self, name: str) -> str | None:
            raise OSError("locked")

        def known_values(self) -> list[str]:
            return []

    scope = secondary.with_secrets(Broken()).with_env(secondary.env.with_overlay({ALLOW: "5"}))
    with caplog.at_level(logging.WARNING):
        assert ScopedReader(scope).get(ALLOW) == "5"
    assert ALLOW in caplog.text and "locked" not in caplog.text


# -- binding ---------------------------------------------------------------------------------------


def test_each_activity_runs_under_its_profile(secondary: Scope, fake_clock: FakeClock) -> None:
    binder = ScopeBinder(secondary)
    seen: dict[str, str] = {}

    def who(kind: str) -> str:
        seen[kind] = current_scope().profile_id
        return seen[kind]

    async def as_task(kind: str) -> str:
        return who(kind)

    async def main() -> None:
        assert scope_or_none() is None
        await binder.run_turn(as_task("turn"))
        await binder.deliver_media(as_task("media_delivery"))
        await binder.cron_tick(as_task("cron_tick"))
        await binder.evict(lambda: who("eviction"))
        await binder.finalise(lambda: who("finalisation"))
        later = binder.defer(30, lambda: who("deferred_callback"))
        await fake_clock.run_until_idle()
        assert "deferred_callback" not in seen
        fake_clock.advance(30)
        await later
        thread = binder.thread(who, "background_thread")
        thread.start()
        thread.join(10)
        binder.call(who, "probe")
        env = binder.child_env()
        seen["child_process"] = "b" if env["INDUS_HOME"] == str(secondary.home) else "?"
        assert scope_or_none() is None

    run(main())
    assert set(seen) == set(ACTIVITIES)
    assert set(seen.values()) == {"b"}


def test_child_env_of_a_secondary_lacks_launch_names(launch: Scope, secondary: Scope) -> None:
    assert ScopeBinder(launch).child_env().get(MARKER) == "1"
    child = ScopeBinder(secondary).child_env(session_id="s1")
    assert MARKER not in child and TOKEN not in child
    assert child["INDUS_HOME"] == str(secondary.home)


def test_eviction_writes_memory_into_the_secondary_only(
    launch: Scope, secondary: Scope, root: Path
) -> None:
    def flush() -> None:
        result = MemoryStore.for_scope(current_scope()).apply("memory", [Op("add", "from b")])
        assert result.ok

    run(ScopeBinder(secondary).evict(flush))
    assert "from b" in (secondary.home / "memories" / FILE_NAMES["memory"]).read_text()
    assert not (root / "memories" / FILE_NAMES["memory"]).exists()


def test_probes_before_any_scope_are_quiet_and_rerun_on_the_first_scoped_turn(
    launch: Scope, secondary: Scope, caplog: pytest.LogCaptureFixture
) -> None:
    gate = ProbeGate()
    calls: list[str] = []

    def check() -> str:
        calls.append(current_scope().profile_id)
        return "ok"

    with caplog.at_level(logging.INFO):
        assert gate.run("docker", check) is None
    assert calls == [] and gate.owed() == ["docker"]
    assert not [r for r in caplog.records if r.levelno >= logging.INFO]
    assert gate.on_scoped_turn(secondary) == {"docker": "ok"}
    assert gate.on_scoped_turn(secondary) == {}
    assert gate.on_scoped_turn(launch) == {"docker": "ok"}
    assert calls == ["b", "default"]
    with use_scope(secondary):
        assert gate.run("now", check) == "ok"
    assert calls[-1] == "b"


def test_a_failing_scoped_probe_is_logged_not_raised(
    secondary: Scope, caplog: pytest.LogCaptureFixture
) -> None:
    gate = ProbeGate()

    def broken() -> None:
        raise RuntimeError("nope")

    gate.run("ssh", broken)
    with caplog.at_level(logging.WARNING):
        assert gate.on_scoped_turn(secondary) == {}
    assert "ssh" in caplog.text


# -- inbound media ---------------------------------------------------------------------------------


def test_inbound_media_moves_into_the_routed_profile_cache(
    launch: Scope, secondary: Scope, root: Path, tmp_path: Path
) -> None:
    cached = root / "cache" / "images" / "a.png"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"png")
    clash = secondary.home / "cache" / "images" / "a.png"
    clash.parent.mkdir(parents=True)
    clash.write_bytes(b"older")
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("x")
    event = InboundEvent(
        source=src("telegram", "1", user_id="1"),
        media=(
            Attachment(ref=str(cached), mime="image/png"),
            Attachment(ref="https://example.invalid/a.png"),
            Attachment(ref=str(outside)),
        ),
    )
    moved = adopt_inbound_media(event, from_home=root, scope=secondary)
    first = Path(moved.media[0].ref)
    assert first == secondary.home / "cache" / "images" / "a-1.png"
    assert first.read_bytes() == b"png" and not cached.exists()
    assert stat.S_IMODE(first.stat().st_mode) == 0o600
    assert clash.read_bytes() == b"older"
    assert moved.media[0].mime == "image/png"
    assert [m.ref for m in moved.media[1:]] == [m.ref for m in event.media[1:]]
    assert adopt_inbound_media(moved, from_home=root, scope=launch) is moved


def test_a_symlinked_attachment_is_not_followed(
    secondary: Scope, root: Path, tmp_path: Path
) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("s")
    link = root / "cache" / "documents" / "link.txt"
    link.parent.mkdir(parents=True)
    link.symlink_to(secret)
    event = InboundEvent(source=src("telegram", "1"), media=(Attachment(ref=str(link)),))
    assert adopt_inbound_media(event, from_home=root, scope=secondary).media[0].ref == str(link)
    assert secret.exists()
