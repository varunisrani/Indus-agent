"""Error codes and id shapes (P01-20)."""

from __future__ import annotations

import ast
import random
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from indus.foundation import errors, ids
from indus.foundation.errors import ERROR_CODES, IndusError
from indus.foundation.ids import IdShape, is_valid_session_id, recognise
from tests.fakes.clock import FakeClock

START = datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)


def _all_error_classes() -> list[type[IndusError]]:
    found: list[type[IndusError]] = []
    pending: list[type[IndusError]] = [IndusError]
    while pending:
        cls = pending.pop()
        found.append(cls)
        pending.extend(cls.__subclasses__())
    return [c for c in found if c.__module__ == errors.__name__]


def test_every_class_has_unique_listed_code() -> None:
    classes = _all_error_classes()
    codes = [c.default_code for c in classes]
    assert len(codes) == len(set(codes))
    assert set(codes) <= set(ERROR_CODES)
    for name in (
        "ConfigUnreadable",
        "ConfigFailedRead",
        "ConfigManaged",
        "ProfileNotFound",
        "InvalidProfileId",
        "ProfileTombstoned",
        "ScopeUnresolved",
        "SecretStoreError",
        "StoreError",
        "ProviderError",
    ):
        assert getattr(errors, name) in classes


def test_codes_are_dotted_lowercase() -> None:
    for code in ERROR_CODES:
        head, _, tail = code.partition(".")
        assert head and tail and code == code.lower() and " " not in code


def test_str_never_includes_detail() -> None:
    err = errors.ConfigUnreadable(
        "config.yaml could not be parsed", detail="line 3: token sk-test-0000", hint="fix line 3"
    )
    assert str(err) == "config.yaml could not be parsed"
    assert "sk-test" not in str(err) and "sk-test" not in repr(err)
    assert err.code == "config.unreadable"
    assert err.hint == "fix line 3"
    assert isinstance(err, errors.ConfigError) and isinstance(err, IndusError)


def test_explicit_code_form() -> None:
    err = IndusError("profile.not_found", "no such profile")
    assert (err.code, str(err)) == ("profile.not_found", "no such profile")
    assert IndusError("plain").code == "indus.error"


def test_every_explicit_code_in_src_is_listed() -> None:
    """``SomeError("x.y", "message")`` anywhere under ``src/`` uses a code from the one table."""
    names = {c.__name__ for c in _all_error_classes()}
    src = Path(errors.__file__).resolve().parents[1]
    unlisted: list[str] = []
    for path in sorted(src.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call) or len(node.args) < 2:
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            first = node.args[0]
            code = first.value if isinstance(first, ast.Constant) else None
            if name in names and isinstance(code, str) and code not in ERROR_CODES:
                unlisted.append(f"{path.relative_to(src)}:{node.lineno} {code}")
    assert unlisted == []


def test_moved_classes_are_the_same_objects() -> None:
    from indus.foundation import secrets
    from indus.foundation.config import errors as config_errors

    assert secrets.SecretStoreUnavailable is errors.SecretStoreUnavailable
    assert errors.SecretStoreUnavailable("no keychain").code == "secrets.unavailable"
    for name in ("ConfigError", "ConfigUnreadable", "ConfigFailedRead", "ConfigManaged"):
        assert getattr(config_errors, name) is getattr(errors, name)


def test_session_id_exact() -> None:
    clock = FakeClock(START)
    assert ids.new_session_id(clock, random.Random(7)) == "20260304_050607_" + format(
        random.Random(7).getrandbits(24), "06x"
    )
    gw = ids.new_session_id(clock, random.Random(1), hex_len=ids.GATEWAY_HEX)
    assert gw.startswith("20260304_050607_") and len(gw.rsplit("_", 1)[1]) == 8
    imp = ids.new_session_id(clock, random.Random(1), hex_len=ids.IMPORT_HEX)
    assert len(imp.rsplit("_", 1)[1]) == 12
    with pytest.raises(ValueError):
        ids.new_session_id(clock, random.Random(1), hex_len=7)


def test_session_stamp_uses_clock_zone() -> None:
    class Zoned:
        def now(self) -> datetime:
            return START.astimezone(timezone(timedelta(hours=5, minutes=30)))

    assert ids.new_session_id(Zoned(), random.Random(0)).startswith("20260304_103607_")


def test_other_mints_exact() -> None:
    clock = FakeClock(START)
    assert ids.new_cron_run_id("job-1", clock) == "cron_job-1_20260304_050607"
    bg = ids.new_background_id(clock, random.Random(3))
    assert bg == "bg_050607_" + format(random.Random(3).getrandbits(24), "06x")
    api = ids.new_api_session_id(clock, random.Random(3))
    assert api == f"api_{int(START.timestamp())}_" + format(random.Random(3).getrandbits(32), "08x")
    uid = ids.new_message_uid(random.Random(9))
    assert uid == format(random.Random(9).getrandbits(128), "032x")
    with pytest.raises(ValueError):
        ids.new_cron_run_id("../x", clock)


@pytest.mark.parametrize(
    ("value", "shape"),
    [
        ("20260304_050607_abcdef", IdShape.SESSION),
        ("20260304_050607_abcdef01", IdShape.SESSION),
        ("20260304_050607_abcdef012345", IdShape.SESSION),
        ("cron_job-1_20260304_050607", IdShape.CRON_RUN),
        ("cron_0a1b2c3d4e5f_20260304_050607", IdShape.CRON_RUN),
        ("bg_050607_abcdef", IdShape.BACKGROUND),
        ("api_1772600767_abcdef01", IdShape.API_SESSION),
        ("0123456789abcdef0123456789abcdef", IdShape.MESSAGE_UID),
    ],
)
def test_every_shape_recognised(value: str, shape: IdShape) -> None:
    assert recognise(value) is shape
    assert is_valid_session_id(value) is (shape is not IdShape.MESSAGE_UID)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "../x",
        "a" * 200,
        "20260304_050607_ABCDEF",
        "20260304_050607_abcde",
        "20260304_050607_abcdef0",
        "20261340_050607_abcdef",
        "20260304_250607_abcdef",
        "20260304_050607_abcdef/../x",
        "cron__20260304_050607",
        "bg_256107_abcdef",
        "api_12_abcdef01",
        "0123456789abcdef0123456789abcdeG",
        " 20260304_050607_abcdef",
        None,
        12345,
    ],
)
def test_garbage_rejected(value: object) -> None:
    assert recognise(value) is None
    assert not is_valid_session_id(value)


def test_minted_ids_always_recognised() -> None:
    # Seeded stand-in for a hypothesis property (hypothesis is not installed in this tree).
    rng = random.Random(20261018)
    for _ in range(2000):
        moment = START + timedelta(seconds=rng.randrange(0, 10 * 365 * 86400))
        clock = FakeClock(moment)
        hex_len = rng.choice(ids.SESSION_HEX_LENGTHS)
        assert recognise(ids.new_session_id(clock, rng, hex_len)) is IdShape.SESSION
        assert recognise(ids.new_background_id(clock, rng)) is IdShape.BACKGROUND
        assert recognise(ids.new_api_session_id(clock, rng)) is IdShape.API_SESSION
        assert recognise(ids.new_message_uid(rng)) is IdShape.MESSAGE_UID
        job = "".join(rng.choice("abc123-._") for _ in range(rng.randint(0, 10)))
        assert recognise(ids.new_cron_run_id("j" + job, clock)) is IdShape.CRON_RUN


def test_default_rng_is_system_random() -> None:
    a, b = ids.new_message_uid(), ids.new_message_uid()
    assert a != b and recognise(a) is IdShape.MESSAGE_UID
