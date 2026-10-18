"""P07-14: `indus sessions` read side: filters, rename, delete, export, pins."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from indus.cli import chat as chat_mod
from indus.cli.session_filters import (
    FilterError,
    SessionFilter,
    build_filter,
    parse_duration,
    parse_when,
)
from indus.foundation.scope import Scope
from indus.state import browse, connection, registry, sessions
from tests.unit.cli.commands._helpers_d import (  # noqa: F401
    DAY,
    aged,
    fake_clock,
    run,
    seeded,
    store_globals,
)
from tests.unit.state._helpers_d import add_message, add_session, meta_value

NOW = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)


def _fake_secret() -> str:
    return "sk" + "-" + "Zq9" * 12


# -- filters --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("30s", 30.0),
        ("5m", 300.0),
        ("5h", 18_000.0),
        ("2d", 2 * DAY),
        ("1w", 7 * DAY),
        ("3", 3 * DAY),
        (" 1.5d ", 1.5 * DAY),
    ],
)
def test_duration_table(text: str, seconds: float) -> None:
    assert parse_duration(text) == pytest.approx(seconds)


@pytest.mark.parametrize("text", ["", "5x", "-2d", "d", "five days", "2 days"])
def test_bad_durations(text: str) -> None:
    with pytest.raises(FilterError):
        parse_duration(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2d", NOW - timedelta(days=2)),
        ("2026-07-05", datetime(2026, 7, 5, tzinfo=UTC)),
        ("2026-07-05 14:30", datetime(2026, 7, 5, 14, 30, tzinfo=UTC)),
        ("2026-07-05T14:30:00+02:00", datetime(2026, 7, 5, 12, 30, tzinfo=UTC)),
    ],
)
def test_when_table(text: str, expected: datetime) -> None:
    assert parse_when(text, NOW) == pytest.approx(expected.timestamp())


def test_naive_dates_use_the_clock_zone() -> None:
    zone = timezone(timedelta(hours=5, minutes=30))
    got = parse_when("2026-07-05", NOW.astimezone(zone))
    assert got == datetime(2026, 7, 5, tzinfo=zone).timestamp()


def test_build_filter_names_the_bad_flag() -> None:
    with pytest.raises(FilterError, match="--before"):
        build_filter({"before": "yesterday-ish"}, NOW)
    with pytest.raises(FilterError, match="--min-messages"):
        build_filter({"min_messages": -1}, NOW)


def test_build_filter_and_its_shape() -> None:
    flt = build_filter({"older_than": "2d", "source": ("cli", " ")}, NOW)
    assert flt.older_than == pytest.approx((NOW - timedelta(days=2)).timestamp())
    assert flt.sources == ("cli",)
    assert flt.only_age_and_source and not flt.empty
    assert SessionFilter().empty
    assert not build_filter({"title": "x"}, NOW).only_age_and_source


# -- read-only listing (R03.16) -------------------------------------------------------------------


def test_list_without_a_store_creates_nothing(scope: Scope) -> None:
    result = run(scope, "sessions", "list")
    assert result.code == 0
    assert "No sessions yet" in result.out
    assert not connection.default_store_path(scope).exists()


def test_list_opens_read_only_and_changes_nothing(
    scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    with seeded(scope) as store:
        aged(store, "s-one", 1, title="first talk")
        path = store.path
    before = path.read_bytes()
    uris: list[str] = []
    real = browse._connect  # pyright: ignore[reportPrivateUsage]

    def spy(target: Path) -> sqlite3.Connection:
        from indus.state.readpool import read_only_uri

        uris.append(read_only_uri(target))
        return real(target)

    monkeypatch.setattr(browse, "_connect", spy)
    result = run(scope, "sessions", "list")
    assert result.code == 0, result.err
    assert "first talk" in result.out
    assert uris and all(u.endswith("?mode=ro") for u in uris)
    assert registry.live_handle_count(path) == 0
    assert path.read_bytes() == before


def test_internal_sources_hidden_unless_named(scope: Scope) -> None:
    with seeded(scope) as store:
        aged(store, "human", 1, title="mine")
        aged(store, "robot", 1, title="sub run", source="subagent")
    plain = run(scope, "sessions", "list")
    assert "mine" in plain.out and "sub run" not in plain.out
    named = run(scope, "sessions", "list", "--source", "subagent")
    assert "sub run" in named.out and "mine" not in named.out


def test_list_limit_and_workspace(scope: Scope) -> None:
    with seeded(scope) as store:
        aged(store, "a", 3, title="alpha", cwd="/work/alpha")
        aged(store, "b", 2, title="beta", cwd="/work/beta")
        aged(store, "c", 1, title="gamma", cwd="/work/alpha/sub")
    limited = run(scope, "sessions", "list", "--limit", "1")
    assert "gamma" in limited.out and "beta" not in limited.out
    ws = run(scope, "sessions", "list", "--workspace", "alpha")
    assert "alpha" in ws.out and "gamma" in ws.out and "beta" not in ws.out


def test_unreadable_store_exits_1(scope: Scope) -> None:
    path = connection.default_store_path(scope)
    path.parent.mkdir(parents=True, exist_ok=True)
    sqlite3.connect(path).close()  # an empty SQLite file: never set up
    result = run(scope, "sessions", "list")
    assert result.code == 1
    assert "cannot be read" in result.err


# -- rename / delete / pins -----------------------------------------------------------------------


def test_rename_by_prefix(scope: Scope) -> None:
    with seeded(scope) as store:
        aged(store, "20260101_aaaa", 1)
    result = run(scope, "sessions", "rename", "20260101_a", "My", "new", "title")
    assert result.code == 0, result.err
    with seeded(scope) as store:
        assert sessions.get(store, "20260101_aaaa")["title"] == "My new title"  # type: ignore[index]


def test_rename_unknown_exits_1(scope: Scope) -> None:
    with seeded(scope) as store:
        aged(store, "x1", 1)
    result = run(scope, "sessions", "rename", "nope", "t")
    assert result.code == 1 and "No session found" in result.err


def test_delete_without_yes_on_non_tty_exits_1(scope: Scope) -> None:
    with seeded(scope) as store:
        aged(store, "doomed", 1, title="doomed talk")
    result = run(scope, "sessions", "delete", "doomed")
    assert result.code == 1
    assert "-y" in result.err
    with seeded(scope) as store:
        assert sessions.get(store, "doomed") is not None


def test_delete_with_yes_tombstones_and_cascades(scope: Scope) -> None:
    with seeded(scope) as store:
        aged(store, "parent", 1)
        aged(store, "kid", 1, source="subagent", parent="parent", marker="_delegate_from")
    result = run(scope, "sessions", "delete", "parent", "-y")
    assert result.code == 0, result.err
    assert "1 linked session" in result.out
    with seeded(scope) as store:
        assert sessions.get(store, "parent") is None
        assert sessions.get(store, "kid") is None
        assert meta_value(store, sessions.TOMBSTONE_PREFIX + "parent") is not None


def test_delete_asks_on_a_terminal(scope: Scope) -> None:
    with seeded(scope) as store:
        aged(store, "keep-me", 1)
    no = run(scope, "sessions", "delete", "keep-me", tty=True, input="n\n")
    assert no.code == 0 and "Kept" in no.out
    yes = run(scope, "sessions", "delete", "keep-me", tty=True, input="y\n")
    assert yes.code == 0 and "Deleted" in yes.out


def test_pin_unpin_and_pinned(scope: Scope) -> None:
    with seeded(scope) as store:
        aged(store, "p1", 1, title="keeper")
        aged(store, "p2", 1, title="other")
    assert run(scope, "sessions", "pin", "p1").code == 0
    listed = run(scope, "sessions", "pinned")
    assert "keeper" in listed.out and "other" not in listed.out
    assert run(scope, "sessions", "unpin", "p1").code == 0
    assert "No pinned sessions" in run(scope, "sessions", "pinned").out
    missing = run(scope, "sessions", "pin", "p2", "ghost")
    assert missing.code == 1 and "ghost" in missing.err


def test_stats(scope: Scope) -> None:
    with seeded(scope) as store:
        aged(store, "s1", 1, messages=3)
        aged(store, "s2", 1, messages=2, source="telegram")
    result = run(scope, "sessions", "stats")
    assert result.code == 0, result.err
    assert "Sessions:   2" in result.out
    assert "Messages:   5" in result.out
    assert "telegram" in result.out


# -- export ---------------------------------------------------------------------------------------


def _secret_session(scope: Scope, secret: str) -> None:
    with seeded(scope) as store:
        add_session(store, "leaky", started_at=store.scope.clock.now().timestamp() - 60)
        add_message(store, "leaky", f"my key is {secret} please keep it")
        add_message(store, "leaky", "noted", role="assistant")


def test_export_redacts_by_default(scope: Scope, tmp_path: Path) -> None:
    secret = _fake_secret()
    _secret_session(scope, secret)
    out = tmp_path / "out.jsonl"
    result = run(scope, "sessions", "export", str(out))
    assert result.code == 0, result.err
    text = out.read_text()
    assert secret not in text
    lines = [json.loads(line) for line in text.splitlines()]
    assert lines[0]["kind"] == "session" and lines[0]["id"] == "leaky"
    assert [line["kind"] for line in lines[1:]] == ["message", "message"]


def test_export_no_redact_keeps_the_text(scope: Scope, tmp_path: Path) -> None:
    secret = _fake_secret()
    _secret_session(scope, secret)
    out = tmp_path / "raw.jsonl"
    result = run(scope, "sessions", "export", str(out), "--no-redact")
    assert result.code == 0, result.err
    assert secret in out.read_text()
    assert "not redacted" in result.out


def test_export_refuses_to_overwrite_without_force(scope: Scope, tmp_path: Path) -> None:
    _secret_session(scope, _fake_secret())
    out = tmp_path / "x.jsonl"
    out.write_text("keep")
    refused = run(scope, "sessions", "export", str(out))
    assert refused.code == 1 and out.read_text() == "keep"
    forced = run(scope, "sessions", "export", str(out), "--force")
    assert forced.code == 0 and out.read_text() != "keep"


def test_export_markdown_and_html_per_session(scope: Scope, tmp_path: Path) -> None:
    secret = _fake_secret()
    _secret_session(scope, secret)
    with seeded(scope) as store:
        aged(store, "second", 1, title="Second <b>talk</b>", content="hello & bye")
    folder = tmp_path / "md"
    md = run(scope, "sessions", "export", str(folder), "--format", "md")
    assert md.code == 0, md.err
    files = sorted(p.name for p in folder.iterdir())
    assert files == ["leaky.md", "second.md"]
    assert secret not in (folder / "leaky.md").read_text()
    single = tmp_path / "one.html"
    html_run = run(
        scope, "sessions", "export", str(single), "--format", "html", "--session-id", "second"
    )
    assert html_run.code == 0, html_run.err
    page = single.read_text()
    assert "Second &lt;b&gt;talk&lt;/b&gt;" in page and "hello &amp; bye" in page


def test_export_jsonl_to_stdout_and_filters(scope: Scope) -> None:
    with seeded(scope) as store:
        aged(store, "old", 30, title="old one")
        aged(store, "new", 1, title="new one")
    result = run(scope, "sessions", "export", "-", "--older-than", "7d")
    assert result.code == 0, result.err
    ids = [json.loads(line)["id"] for line in result.out.splitlines() if '"session"' in line]
    assert ids == ["old"]
    dry = run(scope, "sessions", "export", "--dry-run", "--title", "new")
    assert "Would export 1" in dry.out and "new" in dry.out


def test_export_bad_filter_is_a_usage_error(scope: Scope) -> None:
    with seeded(scope) as store:
        aged(store, "a", 1)
    result = run(scope, "sessions", "export", "-", "--after", "soonish")
    assert result.code == 2 and "--after" in result.err


# -- browse ---------------------------------------------------------------------------------------


def test_browse_without_a_terminal_prints_the_list(scope: Scope) -> None:
    with seeded(scope) as store:
        aged(store, "b1", 1, title="browse me")
    result = run(scope, "sessions", "browse")
    assert result.code == 0 and "browse me" in result.out


def test_browse_resumes_the_pick(scope: Scope, monkeypatch: pytest.MonkeyPatch) -> None:
    with seeded(scope) as store:
        aged(store, "pick-1", 2, title="older")
        aged(store, "pick-2", 1, title="newer")
    seen: list[Any] = []

    def fake_run_chat(state: Any, args: Any, chat: Any) -> int:
        seen.append(args.resume)
        return 0

    monkeypatch.setattr(chat_mod, "run_chat", fake_run_chat)
    result = run(scope, "sessions", "browse", tty=True, input="2\n")
    assert result.code == 0, result.err
    assert seen == ["pick-1"]
    cancelled = run(scope, "sessions", "browse", tty=True, input="\n")
    assert cancelled.code == 0 and seen == ["pick-1"]
