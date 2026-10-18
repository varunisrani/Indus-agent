"""Clock protocol, system clock and zone resolution (P01-19)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path

import pytest

from indus.foundation import clock as clock_mod
from indus.foundation.clock import Clock, SystemClock, resolve_zone, safe_strftime
from indus.foundation.scope import Scope, make_scope
from tests.fakes.clock import FakeClock


class _Config:
    def __init__(self, values: dict[str, object]) -> None:
        self.values = values

    def get(self, path: str) -> object:
        return self.values.get(path)


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    clock_mod.reset_zone_cache()
    yield
    clock_mod.reset_zone_cache()


def _scope(
    tmp_path: Path,
    env: dict[str, str] | None = None,
    config: dict[str, object] | None = None,
    *,
    routed: bool = False,
    pid: str = "work",
) -> Scope:
    s = make_scope(tmp_path / "profiles" / pid, env=env or {}, routed=routed)
    return s.with_config(_Config(config)) if config is not None else s


def test_env_zone_kolkata(tmp_path: Path) -> None:
    zone = resolve_zone(_scope(tmp_path, {"INDUS_TIMEZONE": "Asia/Kolkata"}))
    assert zone is not None
    assert SystemClock(zone).now().utcoffset() == timedelta(hours=5, minutes=30)


def test_env_beats_config(tmp_path: Path) -> None:
    s = _scope(tmp_path, {"INDUS_TIMEZONE": "Asia/Kolkata"}, {"timezone": "Europe/Berlin"})
    zone = resolve_zone(s)
    assert zone is not None and zone.key == "Asia/Kolkata"


def test_config_zone_when_env_absent(tmp_path: Path) -> None:
    zone = resolve_zone(_scope(tmp_path, {}, {"timezone": " America/New_York "}))
    assert zone is not None and zone.key == "America/New_York"


def test_local_when_nothing_set(tmp_path: Path) -> None:
    assert resolve_zone(_scope(tmp_path, {"INDUS_TIMEZONE": "  "}, {"timezone": ""})) is None
    assert SystemClock(None).now().tzinfo is not None


def test_invalid_zone_warns_once(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    s = _scope(tmp_path, {"INDUS_TIMEZONE": "Mars/Olympus_Mons"})
    with caplog.at_level(logging.WARNING, logger="indus.foundation.clock"):
        assert resolve_zone(s) is None
        assert resolve_zone(s) is None
    warnings = [r for r in caplog.records if "Mars/Olympus_Mons" in r.getMessage()]
    assert len(warnings) == 1


def test_routed_scope_ignores_env(tmp_path: Path) -> None:
    routed = _scope(
        tmp_path, {"INDUS_TIMEZONE": "Asia/Kolkata"}, {"timezone": "Europe/Berlin"}, routed=True
    )
    zone = resolve_zone(routed)
    assert zone is not None and zone.key == "Europe/Berlin"
    bare = _scope(tmp_path, {"INDUS_TIMEZONE": "Asia/Kolkata"}, routed=True, pid="other")
    assert resolve_zone(bare) is None


def test_zone_cached_per_profile(tmp_path: Path) -> None:
    a = _scope(tmp_path, {}, {"timezone": "Asia/Tokyo"}, pid="a")
    b = _scope(tmp_path, {}, {"timezone": "Europe/Paris"}, pid="b")
    za, zb = resolve_zone(a), resolve_zone(b)
    assert za is not None and zb is not None
    assert (za.key, zb.key) == ("Asia/Tokyo", "Europe/Paris")
    assert resolve_zone(a) is za


def test_fake_clock_satisfies_protocol() -> None:
    assert isinstance(FakeClock(datetime(2026, 1, 1, tzinfo=UTC)), Clock)
    assert isinstance(SystemClock(), Clock)


def test_system_clock_basics() -> None:
    c = SystemClock()
    first = c.monotonic()
    assert c.monotonic() >= first
    assert c.now().tzinfo is not None
    asyncio.run(c.sleep(0))
    c.sleep_sync(0)
    with pytest.raises(ValueError):
        c.sleep_sync(-1)
    assert repr(c) == "SystemClock(local)"


def test_system_clock_for_scope(tmp_path: Path) -> None:
    c = clock_mod.system_clock_for(_scope(tmp_path, {"INDUS_TIMEZONE": "UTC"}))
    assert c.now().utcoffset() == timedelta(0)


class _SurrogateZone(tzinfo):
    def utcoffset(self, dt: datetime | None) -> timedelta:
        return timedelta(hours=1)

    def dst(self, dt: datetime | None) -> timedelta:
        return timedelta(0)

    def tzname(self, dt: datetime | None) -> str:
        return "heure d'\udce9t\udce9"


def test_safe_strftime_surrogate_zone() -> None:
    value = datetime(2026, 7, 1, 12, 0, tzinfo=_SurrogateZone())
    out = safe_strftime(value, "%Y-%m-%d %H:%M %Z (100%%)")
    assert out == "2026-07-01 12:00 heure d'�t� (100%)"
    out.encode("utf-8")


def test_safe_strftime_plain_is_strftime() -> None:
    value = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    assert safe_strftime(value, "%Y %Z") == value.strftime("%Y %Z")
