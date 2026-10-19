"""Vendor credentials and env hygiene for the cloud backends (P21-11, R02.1, R02.7, R02.22)."""

from __future__ import annotations

import shutil
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from indus.backends.sdk_call import guarded_command
from indus.backends.vendor_env import (
    VENDOR_SOURCE,
    VENDOR_TOKEN_NAMES,
    passthrough_plan,
    register_vendor_names,
    sdk_exec,
    snapshot_excluded_names,
    unset_line,
    vendor_credentials,
    vendor_token_names,
)
from indus.backends.wrapper import ScriptPaths, bootstrap_script, cwd_marker
from indus.foundation.scope import Scope, make_scope
from indus.safety.child_env import (
    STATIC_BLOCKED,
    child_env,
    is_blocked,
    unregister_blocked_names,
)
from tests.fakes.backend.fake_daytona import FakeDaytona
from tests.fakes.backend.sdk_kit import Cloud

REPO = Path(__file__).resolve().parents[3]
GOLDEN = REPO / "tests" / "golden" / "backends" / "snapshot_script.sh"


def forged(tag: str) -> str:
    """A token-shaped value built at run time (never a literal in the repo)."""
    return "fake-" + tag.lower() + "-" + "9" * 12


class Config:
    def __init__(self, values: Mapping[str, Any]) -> None:
        self.values = dict(values)

    def get(self, path: str, /) -> object:
        return self.values.get(path)


class Secrets:
    def __init__(self, values: Mapping[str, str], *, broken: bool = False) -> None:
        self.values = dict(values)
        self.broken = broken

    def get(self, name: str, /) -> str | None:
        if self.broken:
            raise OSError("keychain locked")
        return self.values.get(name)

    def known_values(self) -> list[str]:
        return list(self.values.values())

    @property
    def generation(self) -> int:
        return 1


@pytest.fixture(autouse=True)
def no_registration_leak() -> Iterator[None]:
    yield
    unregister_blocked_names(VENDOR_SOURCE)


def make(
    tmp_path: Path,
    *,
    env: Mapping[str, str] | None = None,
    secrets: Mapping[str, str] | None = None,
    passthrough: list[str] | None = None,
) -> Scope:
    home = tmp_path / "profile"
    home.mkdir(exist_ok=True)
    view = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), **(env or {})}
    scope = make_scope(home, env=view).with_config(
        Config({"terminal.env_passthrough": passthrough or []})
    )
    return scope.with_secrets(Secrets(secrets or {}))


def test_every_vendor_token_is_out_of_every_child_env(tmp_path: Path) -> None:
    every = {name: forged(name) for name in sorted(vendor_token_names())}
    scope = make(tmp_path, env={**every, "KEEP_ME": "yes"}, passthrough=[*every, "KEEP_ME"])
    for registered in (False, True):
        if registered:
            register_vendor_names()
        for variant in ("default", "code", "minimal"):
            built = child_env(scope, variant=variant)  # type: ignore[arg-type]
            leaked = set(built) & set(every)
            assert not leaked, f"{variant} child env carries {sorted(leaked)}"
            assert not set(built.values()) & set(every.values())
        assert child_env(scope)["KEEP_ME"] == "yes"
    assert all(is_blocked(name) for name in every)


def test_static_floor_covers_every_declared_name() -> None:
    sign_in = {
        "MODAL_TOKEN_ID",
        "MODAL_TOKEN_SECRET",
        "DAYTONA_API_KEY",
        "VERCEL_TOKEN",
        "VERCEL_OIDC_TOKEN",
        "VERCEL_PROJECT_ID",
        "VERCEL_TEAM_ID",
    }
    assert sign_in <= vendor_token_names()
    assert vendor_token_names() <= STATIC_BLOCKED  # blocked even before registration runs
    assert set(VENDOR_TOKEN_NAMES) == {"modal", "daytona", "vercel_sandbox"}


def test_vendor_token_in_passthrough_is_refused(tmp_path: Path) -> None:
    scope = make(
        tmp_path,
        env={"MODAL_TOKEN_SECRET": forged("modal"), "MY_VAR": "v"},
        passthrough=["MODAL_TOKEN_SECRET", "MY_VAR"],
    )
    plan = passthrough_plan(scope)
    assert plan.names == ("MY_VAR",) and dict(plan.values) == {"MY_VAR": "v"}


def test_credentials_secret_store_first_then_env(tmp_path: Path) -> None:
    scope = make(
        tmp_path,
        env={"MODAL_TOKEN_ID": "env-id", "MODAL_TOKEN_SECRET": forged("env")},
        secrets={"MODAL_TOKEN_ID": "store-id"},
    )
    creds = vendor_credentials(scope, "modal")
    assert creds.missing == () and creds.reason("modal") is None
    assert creds.values["MODAL_TOKEN_ID"] == "store-id"
    assert creds.values["MODAL_TOKEN_SECRET"] == forged("env")
    assert forged("env") not in repr(creds)


def test_missing_credentials_give_reasons(tmp_path: Path) -> None:
    empty = make(tmp_path)
    modal = vendor_credentials(empty, "modal")
    assert modal.missing == ("MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET")
    assert modal.reason("modal") == (
        "Modal credentials missing: set MODAL_TOKEN_ID and MODAL_TOKEN_SECRET"
    )
    assert vendor_credentials(empty, "daytona").missing == ("DAYTONA_API_KEY",)
    vercel = make(tmp_path, env={"VERCEL_TOKEN": forged("v")})
    assert vendor_credentials(vercel, "vercel_sandbox").missing == (
        "VERCEL_PROJECT_ID",
        "VERCEL_TEAM_ID",
    )
    oidc = make(tmp_path, env={"VERCEL_OIDC_TOKEN": forged("oidc")})
    assert vendor_credentials(oidc, "vercel_sandbox").missing == ()
    jwt = make(tmp_path, secrets={"DAYTONA_JWT_TOKEN": forged("j"), "DAYTONA_ORGANIZATION_ID": "o"})
    assert vendor_credentials(jwt, "daytona").missing == ()
    with pytest.raises(ValueError):
        vendor_credentials(empty, "docker")


def test_broken_secret_store_reads_as_missing(tmp_path: Path) -> None:
    home = tmp_path / "profile"
    home.mkdir()
    scope = make_scope(home, env={"PATH": "/bin"}).with_secrets(Secrets({}, broken=True))
    assert vendor_credentials(scope, "daytona").missing == ("DAYTONA_API_KEY",)


@pytest.mark.skipif(shutil.which("bash") is None, reason="the fake SDK runs bash")
def test_sdk_exec_puts_values_in_env_not_text(tmp_path: Path) -> None:
    value = forged("pass")
    scope = make(
        tmp_path, secrets={"PASS_A": value}, passthrough=["PASS_A", "MISSING_B", "bad name"]
    )
    prepared = sdk_exec(scope, 'printf %s "$PASS_A:${MISSING_B-unset}"')
    assert value not in prepared.command and value not in repr(prepared)
    assert prepared.command.startswith("unset MISSING_B 2>/dev/null || true\n")
    cloud = Cloud(tmp_path / "cloud")
    daytona = FakeDaytona(cloud).module
    sandbox = daytona.Daytona().create(daytona.CreateSandboxFromImageParams(image="debian:12"))
    response = sandbox.process.exec(guarded_command(prepared.command, 10), env=dict(prepared.env))
    assert response.exit_code == 0 and response.result == f"{value}:unset"
    (call,) = cloud.calls_of("Process.exec")
    assert value not in call.args[0]  # R02.7: the command text carries names only
    assert call.kwargs["env"] == {"PASS_A": value}  # the SDK's env parameter carries the value


def test_unset_line_skips_bad_names() -> None:
    assert unset_line([]) == ""
    assert unset_line(["B", "A", "no-dash", "1X"]) == "unset A B 2>/dev/null || true\n"


def _golden_script(tmp_path: Path) -> str:
    scope = make(tmp_path, passthrough=["PROJECT_FLAG", "OTHER_PASS"])
    paths = ScriptPaths(
        snapshot="'/tmp/indus-snap-golden.sh'",
        snapshot_tmp_template="'/tmp/indus-snap-golden.sh.tmp.XXXXXXXXXX'",
        marker=cwd_marker("golden"),
    )
    return bootstrap_script(
        paths, cwd="/vercel/sandbox", excluded_names=snapshot_excluded_names(scope)
    )


def test_snapshot_script_unsets_session_and_passthrough_vars(tmp_path: Path) -> None:
    script = _golden_script(tmp_path)
    assert script == GOLDEN.read_text(encoding="utf-8")
    dump = next(line for line in script.splitlines() if "export -p" in line)
    assert dump.index("unset") < dump.index("export -p")  # unset in the subshell, before the dump
    for word in ("${!INDUS_SESSION_*}", "INDUS_HOME", "AI_AGENT", "OTHER_PASS", "PROJECT_FLAG"):
        assert word in dump


@pytest.mark.skipif(shutil.which("bash") is None, reason="runs the script in bash")
def test_snapshot_script_leaves_session_values_out(tmp_path: Path) -> None:
    import subprocess

    work = tmp_path / "w"
    work.mkdir()
    script = _golden_script(tmp_path).replace("/tmp/indus-snap-golden.sh", f"{work}/snap.sh")
    script = script.replace("/vercel/sandbox", str(work))
    secret = forged("session")
    done = subprocess.run(  # noqa: S603 - the generated script
        ["bash", "-c", script],
        env={
            "PATH": "/usr/bin:/bin",
            "INDUS_SESSION_ID": secret,
            "OTHER_PASS": secret,
            "KEPT_VAR": "kept",
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    snapshot = (work / "snap.sh").read_text(encoding="utf-8")
    assert "KEPT_VAR" in snapshot and secret not in snapshot
