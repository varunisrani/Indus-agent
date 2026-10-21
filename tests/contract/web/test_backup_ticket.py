"""P18-16 follow-up: a single-use download ticket for backup archives (``/api/ops/backup/...``)."""

from __future__ import annotations

import logging
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.config import loader as config_loader
from indus.foundation.scope import Scope
from indus.web.auth.public_paths import TICKET_PATHS
from indus.web.routes import files, system
from tests.contract.web.rest_app import RestApp, build_rest_app

WRITE = {"profile": "work"}
MINT = "/api/ops/backup/download-ticket"
REDEEM = "/api/backups/download"


def _zip(path: Path, body: str = "hello") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("note.txt", body)
    return path


@pytest.fixture
def backups(scope: Scope) -> Path:
    folder = scope.home / "backups"
    folder.mkdir(mode=0o700)
    return folder


@pytest.fixture
def rest(scope: Scope, backups: Path) -> Iterator[RestApp]:
    config_loader.reset()
    # The Files page's root is the backups folder too, so a ticket can be tried on both routes.
    env_scope = scope.with_env(scope.env.with_overlay({files.FILES_ROOT_ENV: str(backups)}))
    built = build_rest_app(env_scope)
    yield built
    built.close()
    config_loader.reset()


def _code(answer: Any) -> str:
    return answer.json()["detail"]["code"]


def _mint(rest: RestApp, archive: Path) -> str:
    minted = rest.client.post(MINT, params=WRITE, json={"archive": str(archive)})
    assert minted.status_code == 200, minted.text
    assert minted.headers["cache-control"] == "no-store"
    assert 0 < minted.json()["expires_in"] <= 60
    return str(minted.json()["ticket"])


def _redeem(caller: Any, archive: Path | str, ticket: str) -> Any:
    return caller.get(REDEEM, params={"archive": str(archive), "ticket": ticket})


def test_the_routes_are_listed() -> None:
    assert ("POST", MINT, True) in system.ROUTE_LIST
    assert ("GET", REDEEM, False) in system.ROUTE_LIST
    assert REDEEM in TICKET_PATHS


def test_a_ticket_downloads_its_archive_once(rest: RestApp, backups: Path) -> None:
    archive = _zip(backups / "indus-backup-1.zip")
    ticket = _mint(rest, archive)
    tab = rest.anonymous()  # a link the browser opens: no header, no cookie
    got = _redeem(tab, archive, ticket)
    assert got.status_code == 200 and got.content == archive.read_bytes()
    assert got.headers["content-disposition"].startswith("attachment")
    assert got.headers["cache-control"] == "no-store"
    again = _redeem(tab, archive, ticket)
    assert again.status_code == 401 and _code(again) == "download_ticket_invalid"


def test_a_ticket_opens_no_other_archive_and_is_spent(rest: RestApp, backups: Path) -> None:
    first = _zip(backups / "a.zip")
    second = _zip(backups / "b.zip", "other")
    ticket = _mint(rest, first)
    tab = rest.anonymous()
    assert _redeem(tab, second, ticket).status_code == 401
    assert _redeem(tab, first, ticket).status_code == 401  # spent by the misuse


def test_file_and_backup_tickets_do_not_cross(rest: RestApp, backups: Path) -> None:
    archive = _zip(backups / "a.zip")
    tab = rest.anonymous()
    backup_ticket = _mint(rest, archive)
    as_file = tab.get("/api/files/download", params={"path": "a.zip", "ticket": backup_ticket})
    assert as_file.status_code == 401
    minted = rest.client.post("/api/files/download-ticket", json={"path": "a.zip"})
    assert minted.status_code == 200, minted.text
    file_ticket = minted.json()["ticket"]
    as_backup = _redeem(tab, archive, file_ticket)
    assert as_backup.status_code == 401 and _code(as_backup) == "download_ticket_invalid"


def test_without_a_good_ticket_nothing_about_the_disk_is_said(rest: RestApp, backups: Path) -> None:
    archive = _zip(backups / "a.zip")
    tab = rest.anonymous()
    answers = [
        tab.get(REDEEM, params=params)
        for params in (
            {"archive": str(archive)},
            {"archive": str(backups / "missing.zip"), "ticket": "x" * 43},
            {"archive": "/etc/passwd", "ticket": "é"},
            {"archive": str(archive), "ticket": "", "profile": "ghost"},
        )
    ]
    assert {a.status_code for a in answers} == {401}
    assert len({a.text for a in answers}) == 1


def test_only_archives_in_the_backups_folder_get_a_ticket(
    rest: RestApp, scope: Scope, tmp_path: Path
) -> None:
    stray = _zip(tmp_path / "elsewhere.zip")
    refused = rest.client.post(MINT, params=WRITE, json={"archive": str(stray)})
    assert refused.status_code == 403 and _code(refused) == "outside_backups"
    note = scope.home / "backups" / "notes.txt"
    note.write_text("not a zip", encoding="utf-8")
    assert rest.client.post(MINT, params=WRITE, json={"archive": str(note)}).status_code == 403
    missing = rest.client.post(MINT, params=WRITE, json={"archive": str(tmp_path / "nope.zip")})
    assert missing.status_code == 404
    assert (
        rest.anonymous().post(MINT, params=WRITE, json={"archive": str(stray)}).status_code == 401
    )


def test_a_link_swapped_in_after_minting_is_refused(
    rest: RestApp, backups: Path, tmp_path: Path
) -> None:
    archive = _zip(backups / "a.zip")
    ticket = _mint(rest, archive)
    archive.unlink()
    outside = _zip(tmp_path / "secret.zip", "secret")
    archive.symlink_to(outside)
    assert _redeem(rest.anonymous(), archive, ticket).status_code == 401


def test_the_header_route_still_works(rest: RestApp, backups: Path) -> None:
    archive = _zip(backups / "a.zip")
    got = rest.client.get("/api/ops/backup/download", params={**WRITE, "archive": str(archive)})
    assert got.status_code == 200 and got.content == archive.read_bytes()


def test_tickets_never_reach_a_log_record(
    rest: RestApp, backups: Path, caplog: pytest.LogCaptureFixture
) -> None:
    archive = _zip(backups / "a.zip")
    caplog.set_level(logging.DEBUG, logger="indus")
    ticket = _mint(rest, archive)
    tab = rest.anonymous()
    _redeem(tab, archive, ticket)
    _redeem(tab, archive, ticket)  # refused: logged by reason only
    assert all(ticket not in record.getMessage() for record in caplog.records)
