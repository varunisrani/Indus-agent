"""Variables reach the container by name only (P11-04, R02.7, R02.22).

The fake client records every call's argv and environment: a value may show up in the client's
environment, never on a command line.
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from indus.backends.docker.env import (
    client_env,
    name_flags,
    plan_env,
    redact_values,
    unset_prefix,
)
from indus.backends.docker.settings import settings_for
from indus.safety.child_env import clear_passthrough, register_passthrough
from tests.fakes.backend.docker_cli import FakeDocker
from tests.fakes.clock import FakeClock
from tests.unit.backends.docker.conftest import Secrets, docker_scope, open_env

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

# Built at run time: no credential-shaped literal in the source.
SECRET = "val-" + "7" * 16
NEWER = "val-" + "8" * 16
PROVIDER_KEY = "sk" + "-" + "z" * 24


@pytest.fixture(autouse=True)
def no_passthrough() -> Iterator[None]:
    clear_passthrough()
    yield
    clear_passthrough()


@pytest.fixture
def work(tmp_path: Path) -> Path:
    path = tmp_path / "work"
    path.mkdir()
    return path


def _argv_text(fake: FakeDocker) -> str:
    return "\n".join(" ".join(c) for c in fake.commands())


def test_plan_names_and_values(tmp_path: Path, fake_clock: FakeClock) -> None:
    secrets = Secrets({"MY_TOKEN": SECRET})
    scope = docker_scope(
        tmp_path,
        None,
        fake_clock,
        env={"FROM_ENV": "env-value", "OPENAI_API_KEY": PROVIDER_KEY},
        secrets=secrets,
        terminal__docker_forward_env=["MY_TOKEN", "FROM_ENV", "ABSENT", "bad name", "MY_TOKEN"],
        terminal__docker_env={"PLAIN": 5, "OPENAI_API_KEY": "x", "LIST": [1]},
    )
    plan = plan_env(scope, settings_for(scope))
    assert plan.exec_names == ("FROM_ENV", "MY_TOKEN")
    assert plan.run_names == ("FROM_ENV", "MY_TOKEN", "PLAIN")
    assert dict(plan.values) == {"MY_TOKEN": SECRET, "FROM_ENV": "env-value", "PLAIN": "5"}
    assert plan.unset == ("ABSENT",)
    assert plan.managed == ("ABSENT", "FROM_ENV", "MY_TOKEN")
    assert plan.refused == ("OPENAI_API_KEY",)


def test_name_flags_never_carry_values() -> None:
    assert name_flags(["B", "A", "B"]) == ["-e", "A", "-e", "B"]


def test_client_env_is_minimal_plus_client_controls(tmp_path: Path, fake_clock: FakeClock) -> None:
    scope = docker_scope(
        tmp_path,
        None,
        fake_clock,
        env={
            "DOCKER_HOST": "unix:///run/user/1/docker.sock",
            "XDG_RUNTIME_DIR": "/run/user/1",
            "OPENAI_API_KEY": PROVIDER_KEY,
            "RANDOM_THING": "x",
        },
    )
    env = client_env(scope, {"MY_TOKEN": SECRET})
    assert env["DOCKER_HOST"].endswith("docker.sock")
    assert env["XDG_RUNTIME_DIR"] == "/run/user/1"
    assert env["MY_TOKEN"] == SECRET
    assert "OPENAI_API_KEY" not in env and "RANDOM_THING" not in env


def test_unset_prefix_and_redaction() -> None:
    assert unset_prefix([]) == ""
    assert unset_prefix(["B", "A", "not valid"]) == "unset A B 2>/dev/null || true\n"
    assert redact_values(f"bad {SECRET}!", [SECRET, "ab"]) == "bad [redacted]!"


def test_values_only_in_the_client_env(
    tmp_path: Path, fake_clock: FakeClock, fake_docker: FakeDocker, work: Path
) -> None:
    scope = docker_scope(
        tmp_path,
        fake_docker,
        fake_clock,
        secrets=Secrets({"MY_TOKEN": SECRET}),
        terminal__docker_forward_env=["MY_TOKEN"],
        terminal__docker_env={"SETTING": "plain-setting"},
    )

    async def go() -> str:
        env = await open_env(scope, fake_docker, work)
        result = await env.execute('printf %s "$MY_TOKEN|$SETTING"')
        await env.cleanup()
        return result.output

    assert asyncio.run(go()) == f"{SECRET}|plain-setting"
    calls = fake_docker.calls()
    run = next(c for c in calls if c["argv"][1:3] == ["run", "-d"])
    argv = run["argv"]
    assert ["-e", "MY_TOKEN"] in [argv[i : i + 2] for i in range(len(argv))]
    assert ["-e", "SETTING"] in [argv[i : i + 2] for i in range(len(argv))]
    assert not any(a.startswith(("MY_TOKEN=", "SETTING=")) for a in argv)
    assert run["env"]["MY_TOKEN"] == SECRET
    execs = [c for c in calls if c["argv"][1] == "exec" and "bash" in c["argv"]]
    assert execs and all(c["env"].get("MY_TOKEN") == SECRET for c in execs)
    assert SECRET not in _argv_text(fake_docker)  # the cmdline check, over every call


def test_blocked_name_never_forwarded(
    tmp_path: Path, fake_clock: FakeClock, fake_docker: FakeDocker, work: Path
) -> None:
    scope = docker_scope(
        tmp_path,
        fake_docker,
        fake_clock,
        env={"OPENAI_API_KEY": PROVIDER_KEY},
        terminal__docker_forward_env=["OPENAI_API_KEY"],
        terminal__env_passthrough=["OPENAI_API_KEY"],
        terminal__docker_env={"INDUS_HOME": "/elsewhere"},
    )
    refused = register_passthrough(["OPENAI_API_KEY"])
    assert refused == ["OPENAI_API_KEY"]

    async def go() -> str:
        env = await open_env(scope, fake_docker, work)
        result = await env.execute('printf %s "${OPENAI_API_KEY-none}"')
        await env.cleanup()
        return result.output

    assert asyncio.run(go()) == "none"
    text = _argv_text(fake_docker)
    assert "-e OPENAI_API_KEY" not in text and "-e INDUS_HOME" not in text
    assert all(PROVIDER_KEY not in str(c["env"]) for c in fake_docker.calls())


def test_exec_reinjects_updated_value(
    tmp_path: Path, fake_clock: FakeClock, fake_docker: FakeDocker, work: Path
) -> None:
    secrets = Secrets({"MY_TOKEN": SECRET})
    scope = docker_scope(
        tmp_path,
        fake_docker,
        fake_clock,
        secrets=secrets,
        terminal__docker_forward_env=["MY_TOKEN"],
    )

    async def go() -> list[str]:
        env = await open_env(scope, fake_docker, work)
        seen = [(await env.execute('printf %s "$MY_TOKEN"')).output]
        secrets.values["MY_TOKEN"] = NEWER
        seen.append((await env.execute('printf %s "$MY_TOKEN"')).output)
        await env.cleanup()
        return seen

    assert asyncio.run(go()) == [SECRET, NEWER]
    assert NEWER not in _argv_text(fake_docker)


def test_names_without_value_are_unset(
    tmp_path: Path, fake_clock: FakeClock, fake_docker: FakeDocker, work: Path
) -> None:
    secrets = Secrets({"MY_TOKEN": SECRET})
    scope = docker_scope(
        tmp_path,
        fake_docker,
        fake_clock,
        secrets=secrets,
        terminal__docker_forward_env=["MY_TOKEN"],
    )

    async def go() -> list[str]:
        env = await open_env(scope, fake_docker, work)  # created with the value baked in
        seen = [(await env.execute('printf %s "${MY_TOKEN-unset}"')).output]
        del secrets.values["MY_TOKEN"]
        seen.append((await env.execute('printf %s "${MY_TOKEN-unset}"')).output)
        await env.cleanup()
        return seen

    assert asyncio.run(go()) == [SECRET, "unset"]


def test_passthrough_names_follow_the_scope(
    tmp_path: Path, fake_clock: FakeClock, fake_docker: FakeDocker, work: Path
) -> None:
    register_passthrough(["SKILL_VAR"])
    scope = docker_scope(tmp_path, fake_docker, fake_clock, env={"SKILL_VAR": "from-skill"})

    async def go() -> str:
        env = await open_env(scope, fake_docker, work)
        result = await env.execute('printf %s "$SKILL_VAR"')
        await env.cleanup()
        return result.output

    assert asyncio.run(go()) == "from-skill"
    assert "from-skill" not in _argv_text(fake_docker)


def test_forwarded_values_stay_out_of_the_snapshot(
    tmp_path: Path, fake_clock: FakeClock, fake_docker: FakeDocker, work: Path
) -> None:
    scope = docker_scope(
        tmp_path,
        fake_docker,
        fake_clock,
        secrets=Secrets({"MY_TOKEN": SECRET + "\n$(touch pwned)"}),
        terminal__docker_forward_env=["MY_TOKEN"],
    )

    async def go() -> tuple[str, bool]:
        env = await open_env(scope, fake_docker, work)
        await env.execute("export OTHER=kept")
        snapshot = Path(env.snapshot_path).read_text(encoding="utf-8")
        again = await env.execute('printf %s "$OTHER"')
        await env.cleanup()
        return snapshot, again.output == "kept"

    snapshot, kept = asyncio.run(go())
    assert kept and "OTHER" in snapshot
    assert "MY_TOKEN" not in snapshot and SECRET not in snapshot  # R02.1: never frozen
    assert not (work / "pwned").exists()
