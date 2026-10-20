"""P07-19: `indus doctor` — seeded faults, --fix, exit codes, --live probes, --ack, --minimal."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import respx

from indus.cli import doctor
from indus.cli.commands.doctor import selection_source
from indus.cli.doctor import DoctorContext, Finding
from indus.foundation import config as config_mod
from indus.foundation import layout
from indus.foundation.config import loader as config_loader
from indus.foundation.config import schema
from indus.foundation.scope import EnvView, Scope
from indus.state import doctor_report
from tests.unit.cli.commands._helpers_d import (  # noqa: F401
    aged,
    fake_clock,
    run,
    seeded,
    store_globals,
)

KEY = "sk-or-" + "v1-" + "Tt5" * 14
MODELS = "https://openrouter.ai/api/v1/models"


@pytest.fixture
def healthy(scope: Scope) -> Scope:
    """A set-up profile: skeleton, config, a provider key, a store, owner-only modes."""
    layout.ensure_home(scope)
    home = scope.home
    (home / "config.yaml").write_text(
        "_config_version: 1\nmodel:\n  default: test-model-1\n  provider: openrouter\n"
    )
    (home / ".env").write_text("SOME_SETTING=on\n")
    ready = scope.with_env(EnvView({"INDUS_HOME": str(home), "OPENROUTER_API_KEY": KEY}))
    with seeded(ready) as store:
        aged(store, "s1", 1)
    os.chmod(home, 0o700)
    for name in ("config.yaml", ".env", "state.db"):
        os.chmod(home / name, 0o600)
    config_loader.invalidate()
    return ready


def _findings(scope: Scope, *, fix: bool = False) -> dict[str, list[Finding]]:
    out: dict[str, list[Finding]] = {}
    for spec, found in doctor.run_checks(DoctorContext(scope, fix=fix)):
        out[spec.id] = found
    return out


def _problems(scope: Scope) -> list[str]:
    return [f.message for items in _findings(scope).values() for f in items if f.problem]


def test_healthy_home_passes(healthy: Scope) -> None:
    result = run(healthy, "doctor")
    assert result.code == 0, result.out
    assert result.out.rstrip().endswith("All checks passed.")
    assert "test-model-1 via openrouter" in result.out
    assert KEY not in result.out


def test_check_order_is_stable() -> None:
    ids = [spec.id for spec in doctor.all_checks()]
    assert ids == [
        "python",
        "packages",
        "home",
        "config",
        "env",
        "keychain",
        "auth",
        "providers",
        "store",
        "backend",
        "remote_backends",  # P21-13
        "external",
        "tools",
        "computer_use",  # P10-20
        "gateway",  # P16-36
    ]


def test_each_seeded_fault_is_named(healthy: Scope) -> None:
    home = healthy.home
    (home / "cron").rmdir()
    os.chmod(home / ".env", 0o644)
    (home / ".env").write_text("LD_PRELOAD=/tmp/x.so\nNAME=café\nOK=1\n")
    (home / "config.yaml").write_text("model: [unclosed\n")
    (home / "auth.json").write_text("{not json")
    os.chmod(home / "auth.json", 0o600)
    config_loader.invalidate()
    problems = "\n".join(_problems(healthy))
    assert "missing folder(s)" in problems and "cron" in problems
    assert ".env can be read by other users" in problems
    assert "sets LD_PRELOAD" in problems
    assert "non-ASCII" in problems and "(NAME)" in problems
    assert "cannot be read" in problems
    assert "auth.json cannot be read" in problems
    result = run(healthy, "doctor")
    assert result.code == 1
    assert "issue(s) left:" in result.out and "Tip: `indus doctor --fix`" in result.out


def test_damaged_store_is_an_error(healthy: Scope) -> None:
    (healthy.home / "state.db").write_bytes(b"SQLite format 3\x00" + b"\xff" * 2000)
    store = _findings(healthy)["store"]
    assert store[0].severity == "error" and "sessions repair" in (store[0].fix or "")


def test_fix_repairs_dirs_modes_and_env(healthy: Scope) -> None:
    home = healthy.home
    (home / "cron").rmdir()
    (home / "logs").rmdir()
    os.chmod(home, 0o755)
    os.chmod(home / "config.yaml", 0o644)
    (home / ".env").write_text("GOOD=1\nLD_PRELOAD=/tmp/x.so\nBAD=“quoted”\n")
    result = run(healthy, "doctor", "--fix")
    assert "Fixed " in result.out, result.out
    assert (home / "cron").is_dir() and (home / "logs").is_dir()
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    assert stat.S_IMODE((home / "config.yaml").stat().st_mode) == 0o600
    assert (home / ".env").read_text() == "GOOD=1\n"
    quarantined = (home / ".env.quarantine").read_text()
    assert "LD_PRELOAD" in quarantined and "BAD=" in quarantined
    again = run(healthy, "doctor")
    assert again.code == 0, again.out


def test_fix_seeds_config_from_the_example_only(healthy: Scope) -> None:
    (healthy.home / "config.yaml").unlink()
    config_loader.invalidate()
    found = _findings(healthy)["config"]
    assert found[0].severity == "warn" and "--fix" in (found[0].fix or "")
    fixed = _findings(healthy, fix=True)["config"]
    assert fixed[0].fixed
    text = (healthy.home / "config.yaml").read_text()
    assert text.startswith(f"{schema.VERSION_KEY}: {schema.CURRENT_VERSION}\n")
    assert text.endswith(schema.render_example())
    lines = text.splitlines()[1:]
    assert all(line.startswith("#") or not line.strip() for line in lines)


def test_config_version_stamps(healthy: Scope) -> None:
    (healthy.home / "config.yaml").write_text("_config_version: 9\n")
    config_loader.invalidate()
    (newer,) = [f for f in _findings(healthy)["config"] if f.id == "config.version"]
    assert newer.severity == "error"
    (healthy.home / "config.yaml").write_text("_config_version: 0\nmodel: {default: 3}\n")
    config_loader.invalidate()
    found = _findings(healthy)["config"]
    assert any(f.id == "config.version" and f.severity == "warn" for f in found)


def test_a_crashing_check_is_contained(healthy: Scope) -> None:
    @doctor.register("zz-broken", "Broken", order=999)
    def broken(ctx: DoctorContext) -> list[Finding]:
        raise RuntimeError("kaput")

    try:
        result = run(healthy, "doctor")
    finally:
        doctor.unregister("zz-broken")
    assert result.code == 1
    assert "the zz-broken check itself failed: RuntimeError: kaput" in result.out
    with pytest.raises(ValueError):
        doctor.register("python", "dup", order=1)(broken)


# -- --live ---------------------------------------------------------------------------------------


@pytest.fixture
def mocked() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        yield router


def test_live_probe_reachable(healthy: Scope, mocked: respx.MockRouter) -> None:
    route = mocked.get(MODELS).mock(return_value=httpx.Response(200, json={"data": []}))
    result = run(healthy, "doctor", "--live")
    assert result.code == 0, result.out
    assert "openrouter: reachable" in result.out
    assert route.called
    assert route.calls[0].request.headers["Authorization"] == f"Bearer {KEY}"
    assert KEY not in result.out


def test_live_probe_uses_the_configured_timeout(healthy: Scope, mocked: respx.MockRouter) -> None:
    (healthy.home / "config.yaml").write_text(
        "model:\n  default: test-model-1\n  provider: openrouter\n"
        "doctor:\n  live_probe_timeout_s: 3\n"
    )
    config_loader.invalidate()
    seen: list[object] = []

    def slow(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions.get("timeout"))
        raise httpx.ReadTimeout("slow", request=request)

    mocked.get(MODELS).mock(side_effect=slow)
    result = run(healthy, "doctor", "--live")
    assert result.code == 1
    assert "openrouter: no answer within 3s" in result.out
    assert seen and seen[0]["read"] == 3.0  # type: ignore[index]


def test_live_probe_http_error(healthy: Scope, mocked: respx.MockRouter) -> None:
    mocked.get(MODELS).mock(return_value=httpx.Response(401))
    result = run(healthy, "doctor", "--live")
    assert result.code == 1 and "HTTP 401" in result.out and "indus auth list" in result.out


def test_without_live_nothing_is_called(healthy: Scope, mocked: respx.MockRouter) -> None:
    route = mocked.get(MODELS).mock(return_value=httpx.Response(200, json={"data": []}))
    assert run(healthy, "doctor").code == 0
    assert not route.called


# -- --ack and --minimal --------------------------------------------------------------------------


def test_ack_records_the_advisory(healthy: Scope) -> None:
    first = run(healthy, "doctor", "--ack", "IA-2026-001")
    assert first.code == 0 and "Acknowledged advisory IA-2026-001" in first.out
    view = config_mod.load(healthy)
    assert view.get("security.acked_advisories") == ["IA-2026-001"]
    assert view.get("model.default") == "test-model-1"
    again = run(healthy, "doctor", "--ack", "IA-2026-001")
    assert "already" in again.out
    bad = run(healthy, "doctor", "--ack", "not an id!")
    assert bad.code == 2


def test_minimal_matches_the_milestone_report(healthy: Scope) -> None:
    expected = doctor_report.minimal_report(healthy, selected_by=selection_source(healthy))
    text = run(healthy, "doctor", "--minimal")
    assert text.code == expected.exit_code == 0
    assert text.out == expected.text() + "\n"
    as_json = run(healthy, "doctor", "--minimal", "--json")
    assert json.loads(as_json.out) == json.loads(json.dumps(expected.as_dict(), default=str))
    assert run(healthy, "doctor", "--json").code == 2


def test_minimal_on_a_fresh_home_creates_no_store(scope: Scope) -> None:
    result = run(scope, "doctor", "--minimal")
    assert result.code == 0
    assert "store: missing" in result.out
    assert not (scope.home / "state.db").exists()


def test_selection_source(scope: Scope, tmp_path: Path) -> None:
    assert selection_source(scope) == "flag or env"
    (scope.root / "active_profile").write_text("work\n")
    assert selection_source(scope) == "sticky"
    assert selection_source(scope, "flag") == "flag"


def test_minimal_reports_how_bootstrap_picked_the_profile(
    scope: Scope, capsys: pytest.CaptureFixture[str]
) -> None:
    from indus.cli import entry
    from indus.foundation import log as logmod

    env = {k: v for k, v in os.environ.items() if not k.startswith("INDUS_")}
    env["INDUS_HOME"] = str(scope.home)
    capsys.readouterr()
    try:
        code = entry.main(["doctor", "--minimal", "--json"], env=env)
    finally:
        logmod.shutdown()  # entry set up process logging; later tests start without it
    assert code in (0, 1)
    assert json.loads(capsys.readouterr().out)["home"]["selected_by"] == "env"
