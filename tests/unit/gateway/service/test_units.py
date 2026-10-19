"""P15-28: golden systemd units and launchd plists, names, quoting, stop budget."""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from indus.service.gateway_units import (
    UnitSpec,
    home_hash,
    launchd_label,
    launchd_plist,
    launchd_wrapper,
    service_name,
    systemd_quote,
    systemd_unit,
    timeout_stop_seconds,
    user_unit_dir,
)

GOLDEN = Path(__file__).parents[3] / "fixtures" / "gateway" / "units"
USER_HOME = "/home/ada"
PROGRAM = ("/opt/indus/bin/indus",)
ENV = {"HOME": USER_HOME, "PATH": "/home/ada/.local/bin:/usr/bin"}


def spec_for(home: str, **settings: Any) -> UnitSpec:
    return UnitSpec.build(
        Path(home),
        env=ENV,
        setting=lambda key, default: settings.get(key.split(".")[-1], default),
        program=PROGRAM,
    )


CASES = {
    "systemd-default.service": lambda: systemd_unit(spec_for(f"{USER_HOME}/.indus")),
    "systemd-profile.service": lambda: systemd_unit(spec_for(f"{USER_HOME}/.indus/profiles/work")),
    "systemd-watchdog.service": lambda: systemd_unit(
        spec_for(f"{USER_HOME}/.indus", systemd_watchdog_seconds=45, cron_drain_timeout=90)
    ),
    "systemd-system.service": lambda: systemd_unit(
        spec_for(f"{USER_HOME}/.indus"), system_user="ada"
    ),
    "launchd-default.plist": lambda: launchd_plist(spec_for(f"{USER_HOME}/.indus")),
    "launchd-profile.plist": lambda: launchd_plist(spec_for(f"{USER_HOME}/.indus/profiles/work")),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_golden(name: str) -> None:
    assert CASES[name]() == (GOLDEN / name).read_text(encoding="utf-8")


def test_names_follow_the_home() -> None:
    default = Path(USER_HOME) / ".indus"
    work = default / "profiles" / "work"
    assert service_name(default, default) == "indus-gateway"
    assert launchd_label(default, default) == "ai.indus.gateway"
    assert service_name(work, default) == f"indus-gateway-{home_hash(work)}"
    assert launchd_label(work, default) == f"ai.indus.gateway.{home_hash(work)}"
    assert len(home_hash(work)) == 8 and home_hash(work) != home_hash(default)
    # A custom root (a temp home, a container volume) never takes the bare name.
    assert service_name(Path("/tmp/x/home"), default).startswith("indus-gateway-")
    spec = spec_for(str(work))
    assert spec.profile == "work" and spec.unit_file.endswith(".service")


@pytest.mark.parametrize(
    ("restart", "cron", "expected"),
    [
        (0, 30, 70),  # defaults: cron 30 + 10 reserve + 30 headroom
        (0, 0, 60),  # floor
        (0, 10, 60),
        (120, 30, 150),  # a long chat drain wins
        (0, 90, 130),
        (-5, -5, 60),
    ],
)
def test_timeout_stop_seconds(restart: float, cron: float, expected: int) -> None:
    assert timeout_stop_seconds(restart, cron) == expected


def test_quoting_neutralises_specifiers_and_expansion() -> None:
    assert systemd_quote('/a b/"x"\\%h$HOME') == '"/a b/\\"x\\"\\\\%%h$$HOME"'
    spec = UnitSpec.build(Path("/srv/odd dir/50%/home"), env=ENV, program=("/opt/in dus/indus",))
    text = systemd_unit(spec)
    assert 'ExecStart="/opt/in dus/indus" "gateway" "run"' in text
    assert 'Environment="INDUS_HOME=/srv/odd dir/50%%/home"' in text
    assert "WorkingDirectory=/srv/odd dir/50%%/home" in text


def test_unit_dir_follows_xdg(tmp_path: Path) -> None:
    assert user_unit_dir({"HOME": "/h"}) == Path("/h/.config/systemd/user")
    assert user_unit_dir({"HOME": "/h", "XDG_CONFIG_HOME": "/c"}) == Path("/c/systemd/user")
    assert user_unit_dir({"HOME": "/h", "XDG_CONFIG_HOME": "rel"}) == Path(
        "/h/.config/systemd/user"
    )


def test_settings_of_the_wrong_type_fall_back() -> None:
    spec = spec_for(f"{USER_HOME}/.indus", systemd_watchdog_seconds="x", cron_drain_timeout=True)
    assert spec.watchdog_s == 0 and spec.timeout_stop_s == 70


def test_plist_shape() -> None:
    agent = plistlib.loads(launchd_plist(spec_for(f"{USER_HOME}/.indus")).encode())
    assert agent["KeepAlive"] == {"SuccessfulExit": False}
    assert agent["ExitTimeOut"] == 60 and agent["RunAtLoad"] is True
    assert agent["ProgramArguments"][:4] == ["/bin/sh", "-c", launchd_wrapper(), "indus-gateway"]
    assert agent["ProgramArguments"][4:] == [*PROGRAM, "gateway", "run"]
    assert agent["EnvironmentVariables"]["INDUS_HOME"] == f"{USER_HOME}/.indus"


@pytest.mark.skipif(not os.path.exists("/bin/sh"), reason="needs /bin/sh")
@pytest.mark.parametrize(("code", "expected"), [(0, 0), (75, 75), (78, 0), (3, 3)])
def test_launchd_wrapper_maps_78_to_0(code: int, expected: int) -> None:
    done = subprocess.run(
        [
            "/bin/sh",
            "-c",
            launchd_wrapper(),
            "indus-gateway",
            sys.executable,
            "-c",
            f"raise SystemExit({code})",
        ],
        check=False,
        timeout=30,
    )
    assert done.returncode == expected


@pytest.mark.skipif(not os.path.exists("/bin/sh"), reason="needs /bin/sh")
def test_launchd_wrapper_forwards_sigterm(tmp_path: Path) -> None:
    marker = tmp_path / "got-term"
    on_term = f"lambda *_: (open({str(marker)!r}, 'w').close(), done.set())"
    child = (
        "import signal, sys, threading\n"
        "done = threading.Event()\n"
        f"signal.signal(signal.SIGTERM, {on_term})\n"
        "print('ready', flush=True)\n"
        "done.wait(20)\n"
        "sys.exit(0)\n"
    )
    proc = subprocess.Popen(
        ["/bin/sh", "-c", launchd_wrapper(), "indus-gateway", sys.executable, "-c", child],
        stdout=subprocess.PIPE,
    )
    try:
        assert proc.stdout is not None and proc.stdout.readline().strip() == b"ready"
        proc.terminate()  # SIGTERM to the wrapper, as launchd sends it
        assert proc.wait(timeout=20) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert marker.exists()


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="systemd-analyze not found")
def test_systemd_analyze_accepts_the_unit(tmp_path: Path) -> None:
    home = tmp_path / "gw-home"
    home.mkdir()
    spec = UnitSpec.build(
        home,
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
        program=(sys.executable, "-m", "indus"),
    )
    unit = tmp_path / spec.unit_file
    unit.write_text(systemd_unit(spec), encoding="utf-8")
    try:
        done = subprocess.run(
            ["systemd-analyze", "verify", "--man=no", str(unit)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f"systemd-analyze could not run here: {exc}")
    complaints = [ln for ln in (done.stderr + done.stdout).splitlines() if spec.unit_file in ln]
    assert done.returncode == 0 and complaints == [], done.stderr
