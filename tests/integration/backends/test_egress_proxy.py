"""The egress proxy on the wire (P24-42), loopback only: fake TLS upstreams with their own test CA,
names resolved to 127.0.0.1 by the test, an ``httpx`` client using the proxy as a container would.

- an allow-listed host gets the real secret in place of the container's token;
- a host off the list is tunnelled untouched (it sees the token, never the secret);
- an unknown token is never swapped; ``block_other_hosts`` refuses other hosts;
- a name that resolves to a non-public address is refused before any connection;
- the detached daemon starts, answers its health check and stops (always stopped).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
import signal
import ssl
import sys
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from indus.backends.docker import egress
from indus.backends.docker.egress import (
    TOKEN_PREFIX,
    CertAuthority,
    EgressProxy,
    ProxyPolicy,
    Rule,
    TokenBook,
)
from indus.foundation import config
from indus.foundation.scope import EnvView, make_scope

cryptography = pytest.importorskip("cryptography")

from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402

ALLOWED = "api.allowed.test"
OTHER = "other.test"
INTERNAL = "metadata.test"


# -- a test CA for the fake upstreams -------------------------------------------------------------


def _name(text: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, text)])


class UpstreamCA:
    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.now(UTC)
        self.cert = (
            x509.CertificateBuilder()
            .subject_name(_name("test upstream CA"))
            .issuer_name(_name("test upstream CA"))
            .public_key(self.key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=2))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(
                x509.KeyUsage(True, False, False, False, False, True, True, False, False),
                critical=True,
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(self.key.public_key()), critical=False
            )
            .sign(self.key, hashes.SHA256())
        )
        self.path = folder / "upstream-ca.pem"
        self.path.write_bytes(self.cert.public_bytes(serialization.Encoding.PEM))

    def server_context(self, host: str) -> ssl.SSLContext:
        key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.now(UTC)
        cert = (
            x509.CertificateBuilder()
            .subject_name(_name(host))
            .issuer_name(self.cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(hours=1))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(self.key.public_key()),
                critical=False,
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
            )
            .sign(self.key, hashes.SHA256())
        )
        chain = self.folder / f"{host}.pem"
        chain.write_bytes(
            cert.public_bytes(serialization.Encoding.PEM)
            + key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(chain))
        return context


@dataclass
class Upstream:
    """A TLS HTTP server that records the request heads it gets and echoes Authorization."""

    host: str
    port: int = 0
    heads: list[str] = field(default_factory=list[str])
    server: asyncio.Server | None = None

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
        except (asyncio.IncompleteReadError, ConnectionError):
            writer.close()
            return
        self.heads.append(head)
        auth = next(
            (
                ln.split(":", 1)[1].strip()
                for ln in head.split("\r\n")
                if ln.lower().startswith("authorization:")
            ),
            "",
        )
        body = f"{self.host} saw {auth}".encode()
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nKeep-Alive: timeout=5\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode()
            + body
        )
        with contextlib.suppress(ConnectionError):
            await writer.drain()
        writer.close()


@dataclass
class Lab:
    proxy: EgressProxy
    proxy_port: int
    allowed: Upstream
    other: Upstream
    token: str
    real: str
    client_tls: ssl.SSLContext
    policy: dict[str, Any]

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            proxy=f"http://127.0.0.1:{self.proxy_port}",
            verify=self.client_tls,
            trust_env=False,
            timeout=10.0,
        )


@contextlib.asynccontextmanager
async def lab(
    tmp_path: Path, *, address_ok: Callable[[str], bool] | None = None
) -> AsyncIterator[Lab]:
    upstream_ca = UpstreamCA(tmp_path)
    allowed = Upstream(ALLOWED)
    other = Upstream(OTHER)
    for upstream in (allowed, other):
        upstream.server = await asyncio.start_server(
            upstream.handle, "127.0.0.1", 0, ssl=upstream_ca.server_context(upstream.host)
        )
        upstream.port = upstream.server.sockets[0].getsockname()[1]
    folder = tmp_path / "egress"
    authority = CertAuthority(folder).ensure()
    book = TokenBook(folder)
    token = book.mint("test")
    real = "real-" + secrets.token_hex(12)
    policy: dict[str, Any] = {"block": False}

    async def resolve(host: str, port: int) -> list[str]:
        if host == INTERNAL:
            return ["169.254.169.254"]
        if host in (ALLOWED, OTHER):
            return ["127.0.0.1"]
        raise OSError("unknown name")

    upstream_tls = ssl.create_default_context(cafile=str(upstream_ca.path))
    proxy = EgressProxy(
        policy=lambda: ProxyPolicy(
            (Rule(ALLOWED, "EXAMPLE_KEY", ("Authorization",)),), (), bool(policy["block"])
        ),
        secret=lambda name: real if name == "EXAMPLE_KEY" else None,
        tokens=book,
        authority=authority,
        upstream_tls=upstream_tls,
        resolve=resolve,
        address_ok=address_ok or (lambda address: address == "127.0.0.1"),
    )
    _, port = await proxy.start("127.0.0.1", 0)
    client_tls = ssl.create_default_context(cafile=str(authority.cert_path))
    client_tls.load_verify_locations(cafile=str(upstream_ca.path))
    try:
        yield Lab(proxy, port, allowed, other, token, real, client_tls, policy)
    finally:
        await proxy.aclose()
        for upstream in (allowed, other):
            if upstream.server is not None:
                upstream.server.close()
                await upstream.server.wait_closed()


def test_allow_listed_host_gets_the_secret_other_hosts_do_not(tmp_path: Path) -> None:
    async def go() -> None:
        async with lab(tmp_path) as box, box.client() as client:
            headers = {"Authorization": f"Bearer {box.token}"}
            first = await client.get(
                f"https://{ALLOWED}:{box.allowed.port}/v1/models", headers=headers
            )
            assert first.status_code == 200
            assert first.text == f"{ALLOWED} saw Bearer {box.real}"
            assert first.headers.get("connection") == "close"
            assert box.proxy.injected == 1
            second = await client.get(f"https://{OTHER}:{box.other.port}/", headers=headers)
            assert second.text == f"{OTHER} saw Bearer {box.token}"  # tunnelled, never opened
            assert box.real not in box.other.heads[0]
            stranger = TOKEN_PREFIX + "1" * 40
            third = await client.get(
                f"https://{ALLOWED}:{box.allowed.port}/v1/models",
                headers={"Authorization": f"Bearer {stranger}"},
            )
            assert third.text == f"{ALLOWED} saw Bearer {stranger}"
            assert box.proxy.injected == 1
            assert all("proxy-connection" not in h.lower() for h in box.allowed.heads)

    asyncio.run(go())


def test_block_other_hosts_refuses_them(tmp_path: Path) -> None:
    async def go() -> None:
        async with lab(tmp_path) as box, box.client() as client:
            box.policy["block"] = True
            with pytest.raises(httpx.ProxyError):
                await client.get(f"https://{OTHER}:{box.other.port}/")
            assert box.other.heads == []
            ok = await client.get(
                f"https://{ALLOWED}:{box.allowed.port}/",
                headers={"Authorization": f"Bearer {box.token}"},
            )
            assert ok.text.endswith(box.real)

    asyncio.run(go())


def test_non_public_address_is_refused(tmp_path: Path) -> None:
    async def go() -> None:
        async with lab(tmp_path, address_ok=egress.public_only) as box, box.client() as client:
            with pytest.raises(httpx.ProxyError):
                await client.get(f"https://{INTERNAL}:443/latest/meta-data")
            with pytest.raises(httpx.ProxyError):  # loopback is not public either
                await client.get(f"https://{OTHER}:{box.other.port}/")
            assert box.other.heads == []

    asyncio.run(go())


def test_health_answers_with_the_instance(tmp_path: Path) -> None:
    async def go() -> None:
        async with lab(tmp_path) as box:
            answer = await asyncio.to_thread(egress.health, "127.0.0.1", box.proxy_port)
            assert answer == box.proxy.instance

    asyncio.run(go())


# -- the daemon -----------------------------------------------------------------------------------


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_daemon_starts_answers_and_stops(tmp_path: Path) -> None:
    home = tmp_path / "profile"
    home.mkdir()
    (home / "config.yaml").write_text(
        "egress:\n  enabled: true\n  bind: 127.0.0.1\n  port: 0\n", encoding="utf-8"
    )
    env = EnvView.from_process().with_overlay({"INDUS_HOME": str(home)})
    scope = make_scope(home, env=env)
    scope = scope.with_config(config.load(scope))
    record: egress.ProxyRecord | None = None
    try:
        record = egress.start_daemon(scope, program=(sys.executable, "-m", "indus"))
        assert record.host == "127.0.0.1" and record.port > 0
        assert egress.running(home) == record
        again = egress.start_daemon(scope, program=(sys.executable, "-m", "indus"))
        assert again.pid == record.pid  # one proxy per profile
        assert (home / "egress" / "ca.pem").is_file()
        assert egress.stop_daemon(home)
        assert egress.running(home) is None
        assert not _alive(record.pid)
    finally:
        if record is not None and _alive(record.pid):
            with contextlib.suppress(ProcessLookupError):
                os.kill(record.pid, signal.SIGKILL)
        egress.stop_daemon(home)
