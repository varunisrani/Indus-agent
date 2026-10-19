"""P21-13: `indus doctor` rows for the cloud and container backends."""

from __future__ import annotations

import importlib.metadata
import os
import stat
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from indus.backends import remote_probes
from indus.cli import doctor
from indus.cli.doctor import DoctorContext, Finding
from indus.cli.doctor import remote_backends as rb
from indus.foundation import config
from indus.foundation.config import loader
from indus.foundation.scope import EnvView, Scope
from indus.foundation.secrets import INDEX_FILE, KeychainBackend, SecretStore
from tests.unit.cli.commands._helpers_d import run, store_globals  # noqa: F401

DAYTONA_KEY = "dk" + "-test-" + "0123456789"
MODAL_ID = "ak" + "-test-" + "id01"
MODAL_SECRET = "as" + "-test-" + "secret01"
VENDOR_DISTS = {"modal", "daytona", "vercel-sandbox"}


class DictKeyring:
    """A keychain that lives in a dict (never the OS keychain)."""

    def __init__(self) -> None:
        self.data: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str, /) -> str | None:
        return self.data.get((service, username))

    def set_password(self, service: str, username: str, password: str, /) -> None:
        self.data[(service, username)] = password

    def delete_password(self, service: str, username: str, /) -> None:
        del self.data[(service, username)]


@pytest.fixture(autouse=True)
def fresh_guards() -> Iterator[None]:
    remote_probes.reset_guards()
    yield
    remote_probes.reset_guards()


@pytest.fixture
def bin_dir(tmp_path: Path) -> Path:
    folder = tmp_path / "bin"
    folder.mkdir()
    return folder


@pytest.fixture
def clean(scope: Scope, bin_dir: Path) -> Scope:
    loader.reset()
    store = SecretStore(KeychainBackend(DictKeyring(), scope.profile_id), scope.home / INDEX_FILE)
    env = EnvView({"INDUS_HOME": str(scope.home), "PATH": f"{bin_dir}{os.pathsep}/usr/bin:/bin"})
    return scope.with_env(env).with_secrets(store)


def _without(*missing: str) -> Callable[[str], object]:
    real = importlib.metadata.distribution

    def lookup(name: str) -> object:
        if name in missing:
            raise importlib.metadata.PackageNotFoundError(name)
        if name in VENDOR_DISTS:
            return object()
        return real(name)

    return lookup


@pytest.fixture
def sdks_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importlib.metadata, "distribution", _without())


def _select(scope: Scope, backend: str, **terminal: object) -> None:
    config.save(scope, {"terminal": {"backend": backend, **terminal}})


def _rows(scope: Scope, check_id: str) -> list[Finding]:
    for spec, found in doctor.run_checks(DoctorContext(scope)):
        if spec.id == check_id:
            return found
    raise AssertionError(f"no {check_id} check")


def _fake_cli(folder: Path, name: str) -> None:
    tool = folder / name
    tool.write_text("#!/bin/sh\nexit 0\n")
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR)


# -- through the command --------------------------------------------------------------------------


def test_doctor_ok_for_a_selected_backend_with_its_key(clean: Scope, sdks_installed: None) -> None:
    _select(clean, "daytona")
    assert isinstance(clean.secrets, SecretStore)
    clean.secrets.set("DAYTONA_API_KEY", DAYTONA_KEY)
    result = run(clean, "doctor")
    out = result.out
    assert "Cloud and container backends" in out
    assert "[ok  ] daytona (selected): can run here" in out
    assert "the daytona SDK is installed" in out
    assert "credentials present: DAYTONA_API_KEY (values not shown)" in out
    assert "sync-back cache dir" in out
    assert DAYTONA_KEY not in out and DAYTONA_KEY not in result.err


def test_doctor_names_the_missing_extra(clean: Scope, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importlib.metadata, "distribution", _without("modal"))
    _select(clean, "modal")
    assert isinstance(clean.secrets, SecretStore)
    clean.secrets.set("MODAL_TOKEN_ID", MODAL_ID)
    clean.secrets.set("MODAL_TOKEN_SECRET", MODAL_SECRET)
    result = run(clean, "doctor")
    assert result.code == 1
    assert "indus-agent[modal]" in result.out
    assert "modal (selected): cannot run here" in result.out
    assert MODAL_SECRET not in result.out and MODAL_ID not in result.out


def test_doctor_names_the_missing_token(clean: Scope, sdks_installed: None) -> None:
    _select(clean, "vercel_sandbox")
    result = run(clean, "doctor")
    assert result.code == 1
    assert "Vercel Sandbox credentials missing" in result.out
    assert "VERCEL_TOKEN" in result.out
    assert "indus setup terminal" in result.out


# -- the rows -------------------------------------------------------------------------------------


def test_one_summary_line_per_backend(clean: Scope, sdks_installed: None) -> None:
    rows = _rows(clean, "remote_backends")
    assert [row.id for row in rows] == [
        "backend.singularity.ready",
        "backend.modal.ready",
        "backend.daytona.ready",
        "backend.vercel_sandbox.ready",
    ]
    assert all(row.severity == "info" for row in rows)  # none selected, none can run: no problem
    by_id = {row.id: row.message for row in rows}
    assert "Apptainer or Singularity not found" in by_id["backend.singularity.ready"]
    assert "MODAL_TOKEN_ID" in by_id["backend.modal.ready"]
    assert "DAYTONA_API_KEY" in by_id["backend.daytona.ready"]


def test_a_modal_settings_refusal_shows_once_credentials_are_there(
    clean: Scope, sdks_installed: None
) -> None:
    _select(clean, "modal", modal_mode="managed")
    assert isinstance(clean.secrets, SecretStore)
    clean.secrets.set("MODAL_TOKEN_ID", MODAL_ID)
    clean.secrets.set("MODAL_TOKEN_SECRET", MODAL_SECRET)
    rows = {row.id: row for row in rb.detail_findings(clean, "modal")}
    assert rows["backend.modal.credentials"].severity == "ok"
    assert rows["backend.modal.settings"].severity == "error"
    assert "D-16.16" in rows["backend.modal.settings"].message


def test_singularity_cli_found_and_scratch_ok(clean: Scope, bin_dir: Path) -> None:
    _fake_cli(bin_dir, "apptainer")
    _select(clean, "singularity")
    rows = {row.id: row for row in _rows(clean, "backend")}
    assert rows["backend.singularity"].severity == "ok"
    assert str(bin_dir / "apptainer") in rows["backend.singularity"].message
    assert rows["backend.singularity.scratch"].severity == "ok"
    assert rows["backend.singularity.disk"].severity == "ok"
    summary = {row.id: row for row in _rows(clean, "remote_backends")}
    assert summary["backend.singularity.ready"].message == "singularity (selected): can run here"


def test_singularity_missing_cli_is_an_error(clean: Scope) -> None:
    _select(clean, "singularity")
    rows = {row.id: row for row in _rows(clean, "backend")}
    assert rows["backend.singularity"].severity == "error"
    assert "Apptainer" in (rows["backend.singularity"].fix or "")


def test_singularity_disk_warning(clean: Scope, bin_dir: Path, tmp_path: Path) -> None:
    _fake_cli(bin_dir, "apptainer")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "big.sif").write_bytes(b"x" * (2 * 1024 * 1024))
    _select(clean, "singularity", scratch_dir=str(scratch), disk_warning_gb=0.001)
    rows = {row.id: row for row in _rows(clean, "backend")}
    assert rows["backend.singularity.disk"].severity == "warn"
    assert "disk_warning_gb" in (rows["backend.singularity.disk"].fix or "")


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs POSIX modes, non-root")
def test_unwritable_scratch_is_an_error(clean: Scope, bin_dir: Path, tmp_path: Path) -> None:
    _fake_cli(bin_dir, "apptainer")
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        _select(clean, "singularity", scratch_dir=str(locked / "scratch"))
        rows = {row.id: row for row in _rows(clean, "backend")}
        assert rows["backend.singularity.scratch"].severity == "error"
    finally:
        locked.chmod(0o700)


def test_writable_problem(tmp_path: Path) -> None:
    assert rb.writable_problem(tmp_path) is None
    assert rb.writable_problem(tmp_path / "a" / "b") is None  # created on first use
    target = tmp_path / "file"
    target.write_text("x")
    assert "not a directory" in (rb.writable_problem(target) or "")


def test_a_scratch_dir_that_cannot_be_written_is_an_error(
    clean: Scope, bin_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_cli(bin_dir, "apptainer")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    _select(clean, "singularity", scratch_dir=str(scratch))
    real = os.access
    monkeypatch.setattr(os, "access", lambda p, m: False if Path(p) == scratch else real(p, m))
    rows = {row.id: row for row in rb.detail_findings(clean, "singularity")}
    assert rows["backend.singularity.scratch"].severity == "error"
    assert "not writable" in rows["backend.singularity.scratch"].message
