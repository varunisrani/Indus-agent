"""P02-22: the M0 demo — ``python -m indus.state.doctor_report`` in a temp home."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from indus.foundation import yamlio
from indus.foundation.scope import EnvView, Scope, make_scope
from indus.state import fts, guards, journal, registry
from indus.state.doctor_report import LABELS, Report, Section, Status, minimal_report
from indus.state.errors import Bucket, copy_for
from tests.conftest import FAKE_CLOCK_START
from tests.fakes.clock import FakeClock

ROOT = Path(__file__).resolve().parents[2]
SECTIONS = ("home", "config", "secrets", "store", "logs")


@pytest.fixture(autouse=True)
def _store_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # Stores made here use WAL whatever the host SQLite is (as in tests/unit/state).
    monkeypatch.setattr(journal, "linked_sqlite_version", lambda: (3, 51, 3))
    fts.reset_capabilities()
    guards.reset_health()
    yield
    registry.close_all()
    guards.reset_health()
    fts.reset_capabilities()


def _run(
    root: Path, *args: str, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("INDUS_")}
    env["INDUS_HOME"] = str(root)
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, "-m", "indus.state.doctor_report", *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=ROOT,
        timeout=120,
        check=False,
    )


def _make_store(home: Path) -> Path:
    """The test helper the M0 demo talks about: a real store with one message."""
    home.mkdir(parents=True, exist_ok=True)
    scope = make_scope(
        home,
        env=EnvView.from_process().with_overlay({"INDUS_HOME": str(home)}),
        clock=FakeClock(FAKE_CLOCK_START),
    )
    with registry.open_unshared(scope) as handle:
        handle.write(
            lambda conn: conn.execute(
                "INSERT INTO sessions(id, source, started_at) VALUES ('s1', 'cli', 1.0)"
            )
        )
        for n in range(60):
            handle.write(
                lambda conn, n=n: conn.execute(
                    "INSERT INTO messages(session_id, role, content, timestamp) "
                    "VALUES ('s1', 'user', ?, 1.0)",
                    (f"row {n} " + "x" * 200,),
                )
            )
        return handle.path


def _damage(path: Path) -> None:
    raw = sqlite3.connect(path)
    try:
        root = int(
            raw.execute("SELECT rootpage FROM sqlite_master WHERE name = 'messages'").fetchone()[0]
        )
        size = int(raw.execute("PRAGMA page_size").fetchone()[0])
    finally:
        raw.close()
    with path.open("r+b") as handle:
        handle.seek((root - 1) * size)
        handle.write(bytes((n * 53 + 7) % 256 for n in range(size)))


def test_fresh_home_reports_a_missing_store_and_creates_none(tmp_path: Path) -> None:
    root = tmp_path / "root"
    done = _run(root)
    assert done.returncode == 0, done.stderr
    out = done.stdout
    assert out.splitlines()[0] == LABELS["title"]
    for prefix in ("home: ", "config: ", "secrets: ", "store: missing", "logs: "):
        assert any(line.startswith(prefix) for line in out.splitlines()), prefix
    assert "profile default" in out and "keychain" in out
    assert not (root / "state.db").exists()
    assert not list(root.glob("state.db*"))


def test_a_created_store_reports_ok_with_wal(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _make_store(root)
    done = _run(root)
    assert done.returncode == 0, done.stderr
    assert "store: ok (wal, schema 1, fts5 yes, trigram yes)" in done.stdout


def test_a_corrupt_store_exits_one_with_the_copy_line(tmp_path: Path) -> None:
    root = tmp_path / "root"
    path = _make_store(root)
    _damage(path)
    before = path.read_bytes()
    done = _run(root)
    assert done.returncode == 1, done.stdout + done.stderr
    corrupt = copy_for(Bucket.CORRUPT)
    assert "store: corrupt" in done.stdout
    assert f"{corrupt.what} {corrupt.todo}" in done.stdout
    assert path.read_bytes() == before  # browsing never repairs or moves the file


def test_json_has_the_five_sections_and_config_sources(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "config.yaml").write_text(
        "_config_version: 1\ndisplay:\n  language: fr\nlogging:\n  level: ${DOCTOR_TEST_LEVEL}\n",
        encoding="utf-8",
    )
    done = _run(root, "--json", extra_env={"DOCTOR_TEST_LEVEL": "DEBUG"})
    assert done.returncode == 0, done.stderr
    data = json.loads(done.stdout)
    assert set(SECTIONS) <= set(data) and data["exit_code"] == 0
    assert data["store"]["state"] == "missing"
    keys = {entry["key"]: entry["source"] for entry in data["config"]["user_keys"]}
    assert keys == {"display.language": "user", "logging.level": "env-expanded"}
    assert data["config"]["version"] == 1 and data["config"]["defaults"] > 0
    assert "DEBUG" not in done.stdout  # names and sources only, never values
    assert data["secrets"]["backend"] in ("file", "keychain")
    assert data["home"]["selected_by"] == "default"


def test_profile_flag_targets_the_work_profile(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _make_store(root)  # the default profile has a store, work does not
    work = root / "profiles" / "work"
    work.mkdir(parents=True)
    (work / "profile.yaml").touch()
    done = _run(root, "-p", "work", "--json")
    assert done.returncode == 0, done.stderr
    data = json.loads(done.stdout)
    assert data["home"]["profile"] == "work" and data["home"]["selected_by"] == "flag"
    assert Path(data["home"]["path"]).resolve() == work.resolve()
    assert Path(data["store"]["path"]) == (work / "state.db").resolve()
    assert data["store"]["state"] == "missing"
    assert not (work / "state.db").exists()


def test_unknown_profile_or_argument_exits_one(tmp_path: Path) -> None:
    root = tmp_path / "root"
    done = _run(root, "-p", "nosuch")
    assert done.returncode == 1 and "doctor could not start" in done.stderr
    done = _run(root, "--frobnicate")
    assert done.returncode == 1 and "usage" in done.stderr


# -- in process ---------------------------------------------------------------------------------


def test_labels_match_the_english_catalogue() -> None:
    catalogue = yamlio.load_safe(ROOT / "locales" / "en.yaml")
    assert catalogue["cli"]["doctor"] == LABELS


@pytest.mark.parametrize(
    ("statuses", "code"),
    [
        (("ok", "ok", "ok", "ok", "ok"), 0),
        (("ok", "warn", "ok", "missing", "ok"), 0),
        (("ok", "ok", "ok", "busy", "warn"), 0),
        (("ok", "ok", "ok", "corrupt", "ok"), 1),
        (("ok", "ok", "ok", "unavailable", "ok"), 1),
    ],
)
def test_exit_code_table(statuses: tuple[Status, ...], code: int) -> None:
    report = Report([Section(name, s) for name, s in zip(SECTIONS, statuses, strict=True)])
    assert report.exit_code == code
    assert report.as_dict()["exit_code"] == code


def test_minimal_report_in_process(scope: Scope) -> None:
    report = minimal_report(scope, selected_by="env")
    assert [s.name for s in report.sections] == list(SECTIONS)
    assert report.section("home").facts["selected_by"] == "env"
    assert report.section("store").status == "missing"
    assert not (scope.home / "state.db").exists()
    with registry.open_unshared(scope) as handle:
        handle.write(lambda conn: fts.detach_in(conn, "test", now=1.0))
    stale = minimal_report(scope)
    assert stale.section("store").status == "warn" and stale.exit_code == 0
    assert copy_for(Bucket.FTS_INDEX).what in stale.text()


def test_unreadable_config_is_a_warning(scope: Scope) -> None:
    (scope.home / "config.yaml").write_text("display: [unclosed\n", encoding="utf-8")
    report = minimal_report(scope)
    assert report.section("config").status == "warn" and report.exit_code == 0
