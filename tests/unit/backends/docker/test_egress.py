"""The egress posture of Docker sandboxes (P24-42): the plan, the conflict guards, the reuse label,
the rules and the token swap. The proxy on the wire is in
``tests/integration/backends/test_egress_proxy.py``."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import shutil
import stat
from pathlib import Path
from typing import Any

import pytest

from indus.backends.docker import egress
from indus.backends.docker.egress import (
    LABEL_EGRESS,
    POSTURE_OFF,
    TOKEN_PREFIX,
    CertAuthority,
    EgressConflict,
    EgressPlan,
    EgressProxy,
    EgressUnavailable,
    ProxyPolicy,
    ProxyRecord,
    Rule,
    TokenBook,
    builtin_rules,
    extra_args_conflicts,
    host_allowed,
    plan_for,
    public_only,
)
from indus.backends.docker.environment import (
    LABEL_AGENT,
    LABEL_ENVIRONMENT,
    LABEL_PROFILE,
    LABEL_TASK,
    DockerEnvironment,
    open_docker,
)
from indus.backends.docker.settings import DockerSettings, settings_for
from indus.foundation.scope import Scope
from tests.fakes.backend.docker_cli import FakeDocker
from tests.fakes.clock import FakeClock
from tests.unit.backends.docker.conftest import Secrets, docker_scope, request_for, track

needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _real_key() -> str:
    return "real-" + secrets.token_hex(12)  # built at run time, never a literal


def _record(port: int = 47900) -> ProxyRecord:
    return ProxyRecord(pid=1, host="127.0.0.1", port=port, instance="test")


def _ensure(_scope: Scope, _es: object) -> ProxyRecord:
    return _record()


def _scope(tmp_path: Path, fake: FakeDocker | None, clock: FakeClock, **config: Any) -> Scope:
    values = {"egress__enabled": True, **config}
    return docker_scope(
        tmp_path, fake, clock, secrets=Secrets({"OPENAI_API_KEY": _real_key()}), **values
    )


# -- the plan -------------------------------------------------------------------------------------


def test_off_by_default(tmp_path: Path, fake_clock: FakeClock) -> None:
    scope = docker_scope(tmp_path, None, fake_clock)
    plan = plan_for(scope, settings_for(scope), ensure=_ensure)
    assert plan == EgressPlan() and plan.label == POSTURE_OFF and not plan.active
    assert plan.values() == {} and plan.names == ()


def test_network_off_means_no_egress(tmp_path: Path, fake_clock: FakeClock) -> None:
    scope = _scope(tmp_path, None, fake_clock, terminal__docker_network=False)
    assert plan_for(scope, settings_for(scope), ensure=_ensure).label == POSTURE_OFF


def test_plan_carries_proxy_ca_and_a_token_per_container(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    scope = _scope(tmp_path, None, fake_clock)
    plan = plan_for(scope, settings_for(scope), ensure=_ensure)
    assert plan.active and len(plan.label) == 24
    assert plan.env["HTTPS_PROXY"] == plan.env["http_proxy"] == "http://host.docker.internal:47900"
    assert plan.env["NO_PROXY"] == "127.0.0.1,localhost,::1"
    assert plan.env["SSL_CERT_FILE"] == egress.BUNDLE_TARGET
    assert plan.env["NODE_EXTRA_CA_CERTS"] == egress.CA_TARGET
    assert plan.host_args == ("--add-host", "host.docker.internal:host-gateway")
    assert plan.mount_args[1].endswith(f"ca.pem:{egress.CA_TARGET}:ro")
    assert plan.token_names == ("OPENAI_API_KEY",)
    first, second = plan.values(), plan.values()
    assert first["OPENAI_API_KEY"].startswith(TOKEN_PREFIX)
    assert first["OPENAI_API_KEY"] != second["OPENAI_API_KEY"]  # minted per container
    book = TokenBook(scope.home / "egress")
    assert book.known(first["OPENAI_API_KEY"]) and book.count() == 2


def test_label_is_stable_and_holds_no_token(tmp_path: Path, fake_clock: FakeClock) -> None:
    scope = _scope(tmp_path, None, fake_clock)
    one = plan_for(scope, settings_for(scope), ensure=_ensure)
    two = plan_for(scope, settings_for(scope), ensure=_ensure)
    assert one.label == two.label
    token = one.values()["OPENAI_API_KEY"]
    assert token not in one.label
    moved = plan_for(scope, settings_for(scope), ensure=lambda _s, _e: _record(48000))
    assert moved.label != one.label  # a different proxy address is a different posture


def test_unavailable_proxy_refuses_under_enforcement(tmp_path: Path, fake_clock: FakeClock) -> None:
    scope = _scope(tmp_path, None, fake_clock)

    def broken(_scope: Scope, _es: object) -> ProxyRecord:
        raise EgressUnavailable("the egress proxy exited at start")

    with pytest.raises(EgressUnavailable):
        plan_for(scope, settings_for(scope), ensure=broken)
    relaxed = _scope(tmp_path, None, fake_clock, egress__enforce=False)
    assert plan_for(relaxed, settings_for(relaxed), ensure=broken).label == POSTURE_OFF


# -- conflicts ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [
        ["-e", "HTTPS_PROXY="],
        ["-e", "HTTPS_PROXY=http://elsewhere:1"],
        ["--env", "https_proxy"],
        ["--env=SSL_CERT_FILE=/x"],
        ["-ite", "NO_PROXY=*"],
        ["-iteREQUESTS_CA_BUNDLE=/x"],
        ["-e", "openai_api_key"],  # folded: a variant spelling of an injected name
        ["--env-file", "/tmp/vars"],  # noqa: S108 - a docker argument, not a file we write
        ["--network", "host"],
        ["--net=host"],
    ],
)
def test_extra_args_overriding_egress_are_refused(
    tmp_path: Path, fake_clock: FakeClock, extra: list[str]
) -> None:
    scope = _scope(tmp_path, None, fake_clock, terminal__docker_extra_args=extra)
    with pytest.raises(EgressConflict) as caught:
        plan_for(scope, settings_for(scope), ensure=_ensure)
    assert "docker_extra_args" in str(caught.value)


def test_harmless_extra_args_pass() -> None:
    assert (
        extra_args_conflicts(["-e", "MY_VAR=1", "--shm-size", "2g", "-it"], {"HTTPS_PROXY"}) == []
    )
    assert extra_args_conflicts(["--", "-e", "HTTPS_PROXY=x"], {"HTTPS_PROXY"}) == []


def test_forward_env_and_docker_env_conflicts(tmp_path: Path, fake_clock: FakeClock) -> None:
    forwarded = _scope(tmp_path, None, fake_clock, terminal__docker_forward_env=["HTTPS_PROXY"])
    with pytest.raises(EgressConflict, match="docker_forward_env"):
        plan_for(forwarded, settings_for(forwarded), ensure=_ensure)
    explicit = _scope(
        tmp_path, None, fake_clock, terminal__docker_env={"SSL_CERT_FILE": "/etc/ssl/x"}
    )
    with pytest.raises(EgressConflict, match="docker_env"):
        plan_for(explicit, settings_for(explicit), ensure=_ensure)


def test_without_enforcement_a_conflict_is_a_warning(
    tmp_path: Path, fake_clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="indus.backends.docker.egress")
    scope = _scope(
        tmp_path,
        None,
        fake_clock,
        egress__enforce=False,
        terminal__docker_extra_args=["-e", "HTTPS_PROXY=x"],
    )
    plan = plan_for(scope, settings_for(scope), ensure=_ensure)
    assert plan.active
    assert any("enforce is off" in r.getMessage() for r in caplog.records)


# -- the container over the fake client -----------------------------------------------------------


def _planner(scope: Scope, settings: DockerSettings) -> EgressPlan:
    return plan_for(scope, settings, ensure=_ensure)


async def _open(scope: Scope, fake: FakeDocker, work: Path) -> DockerEnvironment:
    return track(
        await open_docker(
            request_for(scope, cwd=str(work)),
            runner=fake.runner,
            os_name="linux",
            reaper=lambda *_a: None,
            egress=_planner,
        )
    )


@needs_bash
def test_container_env_holds_no_provider_key(
    tmp_path: Path, fake_clock: FakeClock, fake_docker: FakeDocker
) -> None:
    real = _real_key()
    scope = docker_scope(
        tmp_path,
        fake_docker,
        fake_clock,
        secrets=Secrets({"OPENAI_API_KEY": real}),
        egress__enabled=True,
    )
    work = tmp_path / "work"
    work.mkdir()

    async def go() -> None:
        env = await _open(scope, fake_docker, work)
        await env.cleanup()

    asyncio.run(go())
    (box,) = fake_docker.containers().values()
    assert box["labels"][LABEL_EGRESS] != POSTURE_OFF
    assert real not in json.dumps(box)
    assert box["env"]["OPENAI_API_KEY"].startswith(TOKEN_PREFIX)
    assert box["env"]["HTTPS_PROXY"] == "http://host.docker.internal:47900"
    for call in fake_docker.calls():
        assert real not in json.dumps(call["argv"])
        assert real not in json.dumps(call["env"])
    run = next(c for c in fake_docker.commands() if c[:2] == ["run", "-d"])
    assert "OPENAI_API_KEY" in run and not any(a.startswith("OPENAI_API_KEY=") for a in run)
    assert "--add-host" in run and f"{egress.CA_TARGET}:ro" in " ".join(run)


@needs_bash
def test_container_without_egress_is_not_reused_when_egress_is_on(
    tmp_path: Path, fake_clock: FakeClock, fake_docker: FakeDocker
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    plain = docker_scope(tmp_path, fake_docker, fake_clock)

    async def first() -> str | None:
        env = track(
            await open_docker(
                request_for(plain, cwd=str(work)),
                runner=fake_docker.runner,
                os_name="linux",
                reaper=lambda *_a: None,
                egress=_planner,
            )
        )
        cid = env.container_id
        await env.cleanup()  # persistent: left running for reuse
        return cid

    old = asyncio.run(first())
    (box,) = fake_docker.containers().values()
    assert box["labels"][LABEL_EGRESS] == POSTURE_OFF
    guarded = docker_scope(
        tmp_path,
        fake_docker,
        fake_clock,
        secrets=Secrets({"OPENAI_API_KEY": _real_key()}),
        egress__enabled=True,
    )

    async def second() -> str | None:
        env = await _open(guarded, fake_docker, work)
        cid = env.container_id
        await env.cleanup()
        return cid

    new = asyncio.run(second())
    assert old is not None and new is not None and not new.startswith(old[:12])
    postures = sorted(b["labels"][LABEL_EGRESS] for b in fake_docker.containers().values())
    assert POSTURE_OFF in postures and len(postures) == 2
    for b in fake_docker.containers().values():
        assert {LABEL_AGENT, LABEL_TASK, LABEL_PROFILE, LABEL_ENVIRONMENT} <= set(b["labels"])


@needs_bash
def test_extra_args_conflict_starts_nothing(
    tmp_path: Path, fake_clock: FakeClock, fake_docker: FakeDocker
) -> None:
    scope = docker_scope(
        tmp_path,
        fake_docker,
        fake_clock,
        secrets=Secrets({"OPENAI_API_KEY": _real_key()}),
        egress__enabled=True,
        terminal__docker_extra_args=["-e", "HTTPS_PROXY="],
    )
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(EgressConflict):
        asyncio.run(_open(scope, fake_docker, work))
    assert fake_docker.containers() == {}


# -- rules ----------------------------------------------------------------------------------------


def test_builtin_rules_come_from_the_catalogue() -> None:
    rules = {r.host: r for r in builtin_rules()}
    assert rules["api.openai.com"].secret == "OPENAI_API_KEY"
    assert rules["api.openai.com"].headers == ("Authorization",)
    assert rules["api.anthropic.com"].headers == ("x-api-key", "Authorization")
    assert rules["generativelanguage.googleapis.com"].headers[0] == "x-goog-api-key"
    assert all(not r.host.startswith("127.") for r in rules.values())  # http/local ones left out


def test_rules_are_live_only_while_the_key_resolves(tmp_path: Path, fake_clock: FakeClock) -> None:
    scope = docker_scope(
        tmp_path,
        None,
        fake_clock,
        secrets=Secrets({"ANTHROPIC_API_KEY": _real_key()}),
        egress__rules=[{"host": "*.example.test", "secret": "EXAMPLE_KEY", "header": "X-Key"}],
    )
    live = egress.active_rules(scope)
    assert {r.secret for r in live} == {"ANTHROPIC_API_KEY", "EXAMPLE_KEY"}
    assert egress.check_rule({"host": "bad host", "secret": "X"}) is None
    assert egress.check_rule({"host": "a.test", "secret": "1bad"}) is None


def test_host_patterns() -> None:
    rules = [Rule("api.example.test", "K"), Rule("*.wild.test", "K")]
    assert host_allowed("API.example.test", rules, ())
    assert host_allowed("a.b.wild.test", rules, ())
    assert not host_allowed("wild.test", rules, ())
    assert not host_allowed("example.test", rules, ())
    assert host_allowed("plain.test", (), ("plain.test",))
    assert egress.normalize_host("not a host") is None


def test_only_public_addresses_are_dialled() -> None:
    for blocked in ("127.0.0.1", "10.1.2.3", "169.254.169.254", "::1", "100.64.0.1", "fd00::1"):
        assert not public_only(blocked), blocked
    assert public_only("93.184.216.34")
    assert not public_only("not-an-ip")


# -- tokens and the swap --------------------------------------------------------------------------


def test_token_book_keeps_digests_only(tmp_path: Path) -> None:
    book = TokenBook(tmp_path / "egress")
    token = book.mint("p1")
    text = (tmp_path / "egress" / "tokens.json").read_text()
    assert (
        token not in text
        and book.known(token)
        and not book.known(token[:-1] + ("1" if token.endswith("0") else "0"))
    )
    assert stat.S_IMODE((tmp_path / "egress" / "tokens.json").stat().st_mode) == 0o600


def _proxy(tmp_path: Path, rules: tuple[Rule, ...], value: str) -> EgressProxy:
    return EgressProxy(
        policy=lambda: ProxyPolicy(rules),
        secret=lambda name: value if name == "EXAMPLE_KEY" else None,
        tokens=TokenBook(tmp_path / "egress"),
        authority=CertAuthority(tmp_path / "egress"),
    )


def test_swap_only_registered_tokens_in_named_headers(tmp_path: Path) -> None:
    value = _real_key()
    proxy = _proxy(tmp_path, (Rule("api.example.test", "EXAMPLE_KEY", ("Authorization",)),), value)
    token = proxy.tokens.mint("p1")
    stranger = TOKEN_PREFIX + "0" * 40
    request = egress._Request(  # pyright: ignore[reportPrivateUsage]
        "GET",
        f"/v1/x?key={token}",
        "HTTP/1.1",
        [("Authorization", f"Bearer {token}"), ("X-Other", token), ("X-Stranger", stranger)],
    )
    used = proxy._inject("api.example.test", request, proxy.policy())  # pyright: ignore[reportPrivateUsage]
    assert used == ["EXAMPLE_KEY"]
    assert request.headers[0] == ("Authorization", f"Bearer {value}")
    assert request.headers[1] == ("X-Other", token)  # not a header the rule names
    assert request.target == f"/v1/x?key={value}"
    other = egress._Request(  # pyright: ignore[reportPrivateUsage]
        "GET", "/", "HTTP/1.1", [("Authorization", f"Bearer {stranger}")]
    )
    assert proxy._inject("api.example.test", other, proxy.policy()) == []  # pyright: ignore[reportPrivateUsage]
    assert other.headers[0][1] == f"Bearer {stranger}"
    elsewhere = egress._Request(  # pyright: ignore[reportPrivateUsage]
        "GET", "/", "HTTP/1.1", [("Authorization", f"Bearer {token}")]
    )
    assert proxy._inject("other.test", elsewhere, proxy.policy()) == []  # pyright: ignore[reportPrivateUsage]


def test_certificates(tmp_path: Path) -> None:
    pytest.importorskip("cryptography")
    ca = CertAuthority(tmp_path / "egress").ensure()
    assert stat.S_IMODE(ca.key_path.stat().st_mode) == 0o600
    assert ca.cert_path.read_bytes() in ca.bundle_path.read_bytes()
    first = ca.fingerprint()
    assert CertAuthority(tmp_path / "egress").ensure().fingerprint() == first  # kept, not remade
    assert ca.server_context("api.example.test") is ca.server_context("api.example.test")
    ca.server_context("192.0.2.7")
    assert not any((tmp_path / "egress" / "leaf").iterdir())  # leaf files never left behind


def test_bind_address_rules(
    tmp_path: Path, fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    scope = docker_scope(tmp_path, None, fake_clock)
    es = egress.EgressSettings(bind="0.0.0.0")  # noqa: S104 - the value under test
    with pytest.raises(EgressUnavailable):
        egress.bind_address(scope, es)
    assert egress.bind_address(scope, egress.EgressSettings(bind="127.0.0.1")) == "127.0.0.1"
    monkeypatch.setattr(egress.sys, "platform", "darwin")
    assert egress.bind_address(scope, egress.EgressSettings()) == "127.0.0.1"
