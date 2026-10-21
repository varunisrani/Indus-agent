# pyright: strict
"""Egress credential injection for Docker sandboxes (P24-42).

With ``egress.enabled`` a sandbox never holds a real provider key. A small forward proxy of ours
runs on the host (``indus egress serve``, started detached on demand) and every new container gets:

- ``HTTPS_PROXY``/``HTTP_PROXY`` (both casings) → ``http://host.docker.internal:<port>`` with
  ``--add-host host.docker.internal:host-gateway``, and a loopback-only ``NO_PROXY``;
- our CA mounted read-only, ``SSL_CERT_FILE``/``REQUESTS_CA_BUNDLE``/``CURL_CA_BUNDLE`` naming a
  bundle of the public roots plus our CA, ``NODE_EXTRA_CA_CERTS`` naming our CA, ``INDUS_EGRESS=1``;
- one opaque token, minted for that container, under every key name an injection rule covers.

The proxy decrypts TLS only for allow-listed hosts (a leaf certificate per host signed by our CA).
In such a request it replaces a registered token — in the headers the matching rules name and in
the query string — with the real secret, read from the profile's secret store at that moment.
Every other host is tunnelled untouched (the token is useless there) or refused with
``egress.block_other_hosts``. Upstream addresses that are not public are refused whatever name led
to them. Nothing the proxy logs carries a secret or a token.

Rules: one per provider of our catalogue with an ``https`` endpoint (its host, its first key name,
the header its API reads), live while the key resolves; plus ``egress.rules`` entries
(``{host, secret, header}``) and plain ``egress.hosts`` (intercepted, nothing injected).

Posture: containers carry ``indus-egress=<posture>`` (``off`` or a digest of the proxy URL, the CA
fingerprint, the mounts and the injected names — never a token) and reuse filters on it, so a
container made without the proxy is never reused with it and the reverse (R02.20).

Enforcement (``egress.enforce``, default on): forwarded, passthrough or explicit container
variables naming a proxy/CA variable or an injected key, and extra ``docker run`` arguments that
set one, read an env file or pick a network, refuse the start (``EgressConflict``); off, each is a
warning and the user's value wins. A proxy that is on but cannot run refuses the start under
enforcement. ``docker_network: false`` means no egress at all (posture ``off``).

The token store keeps SHA-256 digests only. The CA key is owner-only. Certificates are made with
the ``cryptography`` library (extra ``egress``), imported only when the proxy is used.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import ipaddress
import json
import logging
import os
import re
import secrets
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from indus.backends.contract import BackendError
from indus.backends.docker.settings import DockerSettings
from indus.foundation.clock import SystemClock
from indus.foundation.config.schema import default_for
from indus.foundation.fsio import FileLock, atomic_write_json
from indus.foundation.scope import Scope
from indus.safety.child_env import child_env, passthrough_names
from indus.safety.url_policy import classify_address

__all__ = [
    "BUNDLE_TARGET",
    "CA_TARGET",
    "HEALTH_PATH",
    "LABEL_EGRESS",
    "POSTURE_OFF",
    "PROXY_ALIAS",
    "CertAuthority",
    "EgressConflict",
    "EgressPlan",
    "EgressProxy",
    "EgressSettings",
    "EgressUnavailable",
    "ProxyPolicy",
    "ProxyRecord",
    "Rule",
    "TokenBook",
    "active_rules",
    "STATE_DIR",
    "bind_address",
    "builtin_rules",
    "check_conflicts",
    "check_rule",
    "egress_settings",
    "extra_args_conflicts",
    "health",
    "host_allowed",
    "normalize_host",
    "plan_for",
    "running",
    "serve",
    "start_daemon",
    "stop_daemon",
]

log = logging.getLogger(__name__)

LABEL_EGRESS = "indus-egress"
POSTURE_OFF = "off"
PROXY_ALIAS = "host.docker.internal"
CA_TARGET = "/etc/ssl/certs/indus-egress-ca.pem"
BUNDLE_TARGET = "/etc/ssl/certs/indus-egress-bundle.pem"
HEALTH_PATH = "/__indus_egress/health"
STATE_DIR = "egress"
RECORD_FILE = "proxy.json"
TOKENS_FILE = "tokens.json"
LOG_FILE = "proxy.log"
TOKEN_PREFIX = "indus-egress-"
TOKEN_RE = re.compile(re.escape(TOKEN_PREFIX) + r"[0-9a-f]{40}")
MAX_TOKENS = 4096
HEAD_LIMIT = 64 * 1024
HEAD_TIMEOUT_S = 30.0
CONNECT_TIMEOUT_S = 15.0
START_WAIT_S = 15.0
STOP_WAIT_S = 5.0
LEAF_DAYS = 7
CA_DAYS = 3650
NO_PROXY = "127.0.0.1,localhost,::1"

PROXY_NAMES = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")
CONTROL_NAMES: frozenset[str] = frozenset(
    {
        *PROXY_NAMES,
        "NO_PROXY",
        "no_proxy",
        "ALL_PROXY",
        "all_proxy",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS",
        "INDUS_EGRESS",
    }
)
"""Variables whose override would route around the proxy or its trust anchor."""

HOP_HEADERS = frozenset(
    {"connection", "keep-alive", "proxy-connection", "proxy-authorization", "te", "upgrade"}
)
_HOST_RE = re.compile(
    r"^(\*\.)?([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$"
)
_HEADER_NAME = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_SECRET_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


class EgressConflict(BackendError):
    """A setting would route the sandbox around the egress proxy; nothing was started."""

    default_code = "backend.docker.egress"


class EgressUnavailable(BackendError):
    """The egress proxy is on but cannot run (no certificate library, no address, no daemon)."""

    default_code = "backend.docker.egress"


# -- settings and rules ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Rule:
    """Inject secret ``secret`` into ``headers`` of requests to ``host`` (``*.x`` = subdomains)."""

    host: str
    secret: str = ""
    headers: tuple[str, ...] = ("Authorization",)
    origin: str = "user"


@dataclass(frozen=True, slots=True)
class EgressSettings:
    enabled: bool = False
    enforce: bool = True
    port: int = 47900
    bind: str = "auto"
    hosts: tuple[str, ...] = ()
    rules: tuple[Rule, ...] = ()
    block_other_hosts: bool = False


def _value(scope: Scope, key: str) -> Any:
    raw = scope.config.get(key) if scope.config is not None else None
    return default_for(key) if raw is None else raw


def _flag(raw: object) -> bool:
    if isinstance(raw, str):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return bool(raw)


def normalize_host(raw: str) -> str | None:
    """A lower-case host pattern (``name.tld`` or ``*.name.tld``) or an IP literal; None if odd."""
    text = raw.strip().lower().rstrip(".")
    if not text:
        return None
    try:
        return str(ipaddress.ip_address(text.strip("[]")))
    except ValueError:
        pass
    return text if _HOST_RE.match(text) and len(text) <= 253 else None


def check_rule(entry: Mapping[str, object]) -> Rule | None:
    """An ``egress.rules`` entry as a ``Rule``; None when its host, secret or header is odd."""
    host = normalize_host(str(entry.get("host") or ""))
    secret = str(entry.get("secret") or "").strip()
    header = str(entry.get("header") or "Authorization").strip()
    if host is None or (secret and not _SECRET_NAME.match(secret)):
        return None
    if not _HEADER_NAME.match(header):
        return None
    return Rule(host, secret, (header,), "user")


def _user_rules(raw: object) -> list[Rule]:
    out: list[Rule] = []
    if not isinstance(raw, list):
        return out
    for item in cast(list[object], raw):
        if not isinstance(item, Mapping):
            continue
        rule = check_rule(cast(Mapping[str, object], item))
        if rule is None:
            log.warning("egress.rules: ignoring an entry with a bad host, secret or header name")
            continue
        out.append(rule)
    return out


def egress_settings(scope: Scope) -> EgressSettings:
    hosts_raw = _value(scope, "egress.hosts")
    hosts: list[str] = []
    for item in cast(list[object], hosts_raw) if isinstance(hosts_raw, list) else []:
        host = normalize_host(str(item))
        if host is not None and host not in hosts:
            hosts.append(host)
    try:
        port = int(_value(scope, "egress.port"))
    except (TypeError, ValueError):
        port = 47900
    return EgressSettings(
        enabled=_flag(_value(scope, "egress.enabled")),
        enforce=_flag(_value(scope, "egress.enforce")),
        port=max(0, min(port, 65535)),
        bind=str(_value(scope, "egress.bind") or "auto").strip() or "auto",
        hosts=tuple(hosts),
        rules=tuple(_user_rules(_value(scope, "egress.rules"))),
        block_other_hosts=_flag(_value(scope, "egress.block_other_hosts")),
    )


def _headers_for(api_mode: str) -> tuple[str, ...]:
    if api_mode == "anthropic_messages":
        return ("x-api-key", "Authorization")
    if api_mode == "gemini":
        return ("x-goog-api-key", "Authorization")
    return ("Authorization",)


def builtin_rules() -> list[Rule]:
    """One rule per catalogue provider with an ``https`` endpoint and a key name."""
    from indus.providers.bundled import PROFILES

    out: list[Rule] = []
    for profile in PROFILES:
        parts = urlsplit(profile.base_url)
        if parts.scheme != "https" or not parts.hostname or not profile.env_vars:
            continue
        host = normalize_host(parts.hostname)
        if host is None:
            continue
        out.append(
            Rule(
                host,
                profile.env_vars[0],
                _headers_for(profile.api_mode),
                f"provider:{profile.name}",
            )
        )
    return out


def _has_secret(scope: Scope, name: str) -> bool:
    value: str | None = None
    if scope.secrets is not None:
        with contextlib.suppress(Exception):
            value = scope.secrets.get(name)
    return bool(value or scope.env.get(name))


def active_rules(scope: Scope, settings: EgressSettings | None = None) -> list[Rule]:
    """Catalogue rules whose key resolves now, then the user's rules."""
    es = settings or egress_settings(scope)
    live = [r for r in builtin_rules() if _has_secret(scope, r.secret)]
    return [*live, *es.rules]


def _host_matches(pattern: str, host: str) -> bool:
    if pattern.startswith("*."):
        return host.endswith(pattern[1:]) and host != pattern[2:]
    return host == pattern


def host_allowed(host: str, rules: Iterable[Rule], hosts: Iterable[str]) -> bool:
    name = normalize_host(host)
    if name is None:
        return False
    return any(_host_matches(r.host, name) for r in rules) or any(
        _host_matches(h, name) for h in hosts
    )


# -- tokens ---------------------------------------------------------------------------------------


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class TokenBook:
    """Per-container tokens, kept as SHA-256 digests in ``<home>/egress/tokens.json`` (0600)."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.path = folder / TOKENS_FILE
        self._lock = threading.Lock()
        self._cache: tuple[float, frozenset[str]] | None = None

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return cast(dict[str, Any], data) if isinstance(data, dict) else {}

    def mint(self, label: str) -> str:
        token = TOKEN_PREFIX + secrets.token_hex(20)
        _private_dir(self.folder)
        with FileLock(self.path.with_suffix(".lock"), timeout=10.0):
            data = self._read()
            rows = data.get("tokens")
            table = cast(dict[str, Any], rows) if isinstance(rows, dict) else {}
            table[_digest(token)] = {"label": label, "created": datetime.now(UTC).isoformat()}
            if len(table) > MAX_TOKENS:
                ordered = sorted(table.items(), key=lambda kv: str(kv[1].get("created", "")))
                table = dict(ordered[-MAX_TOKENS:])
            atomic_write_json(self.path, {"version": 1, "tokens": table}, mode=0o600)
        return token

    def digests(self) -> frozenset[str]:
        try:
            stamp = self.path.stat().st_mtime_ns
        except OSError:
            return frozenset()
        with self._lock:
            if self._cache is not None and self._cache[0] == stamp:
                return self._cache[1]
        rows = self._read().get("tokens")
        found: frozenset[str] = (
            frozenset(str(k) for k in cast(dict[str, Any], rows))
            if isinstance(rows, dict)
            else frozenset()
        )
        with self._lock:
            self._cache = (stamp, found)
        return found

    def known(self, token: str) -> bool:
        return _digest(token) in self.digests()

    def count(self) -> int:
        return len(self.digests())


def _private_dir(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        folder.chmod(0o700)
    return folder


# -- certificates ---------------------------------------------------------------------------------


def _crypto_missing() -> EgressUnavailable:
    return EgressUnavailable(
        "the egress proxy needs the 'cryptography' package for its certificates",
        hint="install the extra: pip install 'indus-agent[egress]'",
    )


class CertAuthority:
    """Our CA (``ca.pem`` + owner-only ``ca-key.pem``), the container bundle and leaf contexts."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.cert_path = folder / "ca.pem"
        self.key_path = folder / "ca-key.pem"
        self.bundle_path = folder / "bundle.pem"
        self._ca: tuple[Any, Any] | None = None
        self._leaves: dict[str, ssl.SSLContext] = {}
        self._lock = threading.Lock()

    def ensure(self) -> CertAuthority:
        """Make the CA when missing and (re)write the bundle; raises ``EgressUnavailable``."""
        try:
            from cryptography import x509
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import ec
            from cryptography.x509.oid import NameOID
        except ImportError:
            raise _crypto_missing() from None
        _private_dir(self.folder)
        if not (self.cert_path.is_file() and self.key_path.is_file()):
            key = ec.generate_private_key(ec.SECP256R1())
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Indus Agent egress CA")])
            usage = dict.fromkeys(_KEY_USAGE_FLAGS, False)
            for flag in ("digital_signature", "key_cert_sign", "crl_sign"):
                usage[flag] = True
            cert = _issue(
                subject=name,
                issuer=name,
                public_key=key.public_key(),
                signer=key,
                valid=(timedelta(days=1), timedelta(days=CA_DAYS)),
                extensions=[
                    (x509.SubjectKeyIdentifier.from_public_key(key.public_key()), False),
                    (x509.KeyUsage(**usage), True),
                    (x509.BasicConstraints(ca=True, path_length=0), True),
                ],
                digest=hashes.SHA256(),
            )
            key_pem = key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
            _write_private(self.key_path, key_pem)
            _write_atomic(self.cert_path, cert.public_bytes(serialization.Encoding.PEM), 0o644)
        self._write_bundle()
        return self

    def _write_bundle(self) -> None:
        roots = b""
        try:
            import certifi

            roots = Path(certifi.where()).read_bytes()
        except (ImportError, OSError):
            log.warning(
                "egress: no public root bundle found; the container bundle holds our CA only"
            )
        ours = self.cert_path.read_bytes()
        content = roots.rstrip(b"\n") + b"\n" + ours if roots else ours
        current = self.bundle_path.read_bytes() if self.bundle_path.is_file() else b""
        if current != content:
            _write_atomic(self.bundle_path, content, 0o644)

    def fingerprint(self) -> str:
        try:
            data = self.cert_path.read_bytes()
        except OSError:
            return ""
        return hashlib.sha256(data).hexdigest()[:16]

    def _load(self) -> tuple[Any, Any]:
        if self._ca is None:
            try:
                from cryptography import x509
                from cryptography.hazmat.primitives import serialization
            except ImportError:
                raise _crypto_missing() from None
            cert = x509.load_pem_x509_certificate(self.cert_path.read_bytes())
            key = serialization.load_pem_private_key(self.key_path.read_bytes(), password=None)
            self._ca = (cert, key)
        return self._ca

    def server_context(self, host: str) -> ssl.SSLContext:
        """A TLS server context presenting a leaf for ``host`` signed by our CA (cached)."""
        with self._lock:
            found = self._leaves.get(host)
            if found is not None:
                return found
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

        ca_cert, ca_key = self._load()
        key = ec.generate_private_key(ec.SECP256R1())
        try:
            alt: x509.GeneralName = x509.IPAddress(ipaddress.ip_address(host))
        except ValueError:
            alt = x509.DNSName(host)
        cert = _issue(
            subject=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host[:64])]),
            issuer=ca_cert.subject,
            public_key=key.public_key(),
            signer=ca_key,
            valid=(timedelta(hours=1), timedelta(days=LEAF_DAYS)),
            extensions=[
                (x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), False),
                (x509.SubjectKeyIdentifier.from_public_key(key.public_key()), False),
                (x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False),
                (x509.BasicConstraints(ca=False, path_length=None), True),
                (x509.SubjectAlternativeName([alt]), False),
            ],
            digest=hashes.SHA256(),
        )
        pem = cert.public_bytes(serialization.Encoding.PEM) + ca_cert.public_bytes(
            serialization.Encoding.PEM
        )
        key_pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        leaf_dir = _private_dir(self.folder / "leaf")
        chain = leaf_dir / f"{secrets.token_hex(8)}.pem"
        _write_private(chain, pem + key_pem)
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(str(chain))
        finally:
            chain.unlink(missing_ok=True)
        with self._lock:
            if len(self._leaves) > 512:
                self._leaves.clear()
            self._leaves[host] = context
        return context


_KEY_USAGE_FLAGS = (
    "digital_signature",
    "content_commitment",
    "key_encipherment",
    "data_encipherment",
    "key_agreement",
    "key_cert_sign",
    "crl_sign",
    "encipher_only",
    "decipher_only",
)


def _issue(
    *,
    subject: Any,
    issuer: Any,
    public_key: Any,
    signer: Any,
    valid: tuple[timedelta, timedelta],
    extensions: Sequence[tuple[Any, bool]],
    digest: Any,
) -> Any:
    """One signed certificate (``valid`` = how far back it starts, how long ahead it lasts)."""
    from cryptography import x509

    moment = datetime.now(UTC)
    builder = x509.CertificateBuilder(
        issuer_name=issuer,
        subject_name=subject,
        public_key=public_key,
        serial_number=x509.random_serial_number(),
        not_valid_before=moment - valid[0],
        not_valid_after=moment + valid[1],
    )
    for extension, critical in extensions:
        builder = builder.add_extension(extension, critical=critical)
    return builder.sign(signer, digest)


def _write_atomic(path: Path, data: bytes, mode: int) -> None:
    temp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(temp, mode)
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def _write_private(path: Path, data: bytes) -> None:
    _write_atomic(path, data, 0o600)


# -- conflicts ------------------------------------------------------------------------------------

_BOOL_SHORT_CHARS = "ditPq"
_LONG_ENV_FLAGS = {"--env": "env", "--env-file": "env-file"}


def _env_flag(arg: str) -> tuple[str, str | None] | None:
    """``("env" | "env-file", inline value)`` when ``arg`` is a ``docker run`` env flag (long form,
    ``--x=v``, or a short chain such as ``-ite NAME=v``); the value is None when the flag takes the
    next argument."""
    head, equals, tail = arg.partition("=")
    long_kind = _LONG_ENV_FLAGS.get(head)
    if long_kind is not None:
        return long_kind, (tail if equals else None)
    if arg[:1] != "-" or arg[:2] == "--":
        return None
    # A short chain: booleans first, then ``e`` takes the rest (or the next argument).
    remainder = arg[1:].lstrip(_BOOL_SHORT_CHARS)
    if not remainder.startswith("e"):
        return None
    return "env", remainder[1:].removeprefix("=") or None


def extra_args_conflicts(extra: Sequence[str], critical: Iterable[str]) -> list[str]:
    """Extra ``docker run`` arguments that could undo the egress plumbing."""
    folded = {name.upper() for name in critical}
    found: list[str] = []
    index = 0
    while index < len(extra):
        arg = extra[index]
        if arg == "--":
            break
        parsed = _env_flag(arg)
        if parsed is not None:
            kind, inline = parsed
            if kind == "env-file":
                found.append("--env-file")
            else:
                value = (
                    inline
                    if inline is not None
                    else (extra[index + 1] if index + 1 < len(extra) else "")
                )
                name = value.split("=", 1)[0]
                if name.upper() in folded:
                    found.append(name)
            index += 1 if inline is not None else 2
            continue
        if arg.partition("=")[0] in ("--network", "--net"):
            found.append(arg)
        index += 1
    return sorted(set(found))


def check_conflicts(
    scope: Scope, settings: DockerSettings, injected: Iterable[str], *, enforce: bool
) -> list[str]:
    """The problems; raises ``EgressConflict`` for any when ``enforce`` (see the module notes)."""
    critical = set(CONTROL_NAMES) | set(injected)
    folded = {name.upper() for name in critical}
    problems: list[str] = []
    forwarded = sorted(n for n in settings.forward_env if n.strip().upper() in folded)
    if forwarded:
        problems.append(f"terminal.docker_forward_env names {forwarded}")
    accepted, _ = passthrough_names(scope)
    passed = sorted(n for n in accepted if n.upper() in folded)
    if passed:
        problems.append(f"the env passthrough names {passed}")
    explicit = sorted(n for n in settings.env if n.upper() in folded)
    if explicit:
        problems.append(f"terminal.docker_env sets {explicit}")
    extra = extra_args_conflicts(settings.extra_args, critical)
    if extra:
        problems.append(f"terminal.docker_extra_args has {extra}")
    if not problems:
        return []
    text = "; ".join(problems)
    if enforce:
        raise EgressConflict(
            f"egress is enforced and {text}: that would route the sandbox around the proxy",
            hint="remove those entries, or set egress.enforce to false to let them win",
        )
    log.warning("egress: %s; egress.enforce is off, so your settings win", text)
    return problems


# -- the plan for one container -------------------------------------------------------------------


@dataclass(frozen=True)
class EgressPlan:
    """What ``docker run`` gets for the egress posture (empty when off)."""

    label: str = POSTURE_OFF
    mount_args: tuple[str, ...] = ()
    host_args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=lambda: {})
    token_names: tuple[str, ...] = ()
    minter: Callable[[], str] | None = None

    @property
    def active(self) -> bool:
        return self.label != POSTURE_OFF

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted({*self.env, *self.token_names}))

    def values(self) -> dict[str, str]:
        """The values for one new container; mints its token (never call for a reused one)."""
        out = dict(self.env)
        if self.token_names and self.minter is not None:
            token = self.minter()
            for name in self.token_names:
                out[name] = token
        return out


def posture_label(
    proxy_url: str, ca_fingerprint: str, mounts: Sequence[str], names: Sequence[str]
) -> str:
    payload = json.dumps(
        {"proxy": proxy_url, "ca": ca_fingerprint, "mounts": list(mounts), "names": sorted(names)},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


type Ensure = Callable[[Scope, EgressSettings], "ProxyRecord"]


def plan_for(scope: Scope, settings: DockerSettings, *, ensure: Ensure | None = None) -> EgressPlan:
    """The egress posture of a new or reused container of ``scope`` (see the module notes)."""
    es = egress_settings(scope)
    if not es.enabled or not settings.network:
        return EgressPlan()
    folder = scope.home / STATE_DIR
    try:
        ca = CertAuthority(folder).ensure()
        record = (ensure or ensure_running)(scope, es)
    except (EgressUnavailable, OSError) as exc:
        if es.enforce:
            if isinstance(exc, EgressUnavailable):
                raise
            raise EgressUnavailable(
                f"the egress proxy could not be prepared ({type(exc).__name__})",
                hint="run `indus egress status`, or turn egress.enforce off",
            ) from None
        log.warning("egress is on but unavailable (%s); the sandbox runs without it", exc)
        return EgressPlan()
    rules = active_rules(scope, es)
    token_names = tuple(sorted({r.secret for r in rules if r.secret}))
    proxy_url = f"http://{PROXY_ALIAS}:{record.port}"
    env = {name: proxy_url for name in PROXY_NAMES}
    env.update(
        {
            "NO_PROXY": NO_PROXY,
            "no_proxy": NO_PROXY,
            "SSL_CERT_FILE": BUNDLE_TARGET,
            "REQUESTS_CA_BUNDLE": BUNDLE_TARGET,
            "CURL_CA_BUNDLE": BUNDLE_TARGET,
            "NODE_EXTRA_CA_CERTS": CA_TARGET,
            "INDUS_EGRESS": "1",
        }
    )
    mounts = (
        "-v",
        f"{ca.cert_path}:{CA_TARGET}:ro",
        "-v",
        f"{ca.bundle_path}:{BUNDLE_TARGET}:ro",
    )
    check_conflicts(scope, settings, [*env, *token_names], enforce=es.enforce)
    label = posture_label(proxy_url, ca.fingerprint(), mounts, [*env, *token_names])
    book = TokenBook(folder)
    return EgressPlan(
        label=label,
        mount_args=mounts,
        host_args=("--add-host", f"{PROXY_ALIAS}:host-gateway"),
        env=env,
        token_names=token_names,
        minter=lambda: book.mint(scope.profile_id),
    )


# -- the proxy ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ProxyPolicy:
    rules: tuple[Rule, ...] = ()
    hosts: tuple[str, ...] = ()
    block_other_hosts: bool = False


type Resolve = Callable[[str, int], Awaitable[list[str]]]


async def system_resolve(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def public_only(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    return classify_address(ip) is None


@dataclass
class _Request:
    method: str
    target: str
    version: str
    headers: list[tuple[str, str]]

    def header(self, name: str) -> str | None:
        wanted = name.lower()
        return next((v for k, v in self.headers if k.lower() == wanted), None)

    def encode(self) -> bytes:
        lines = [f"{self.method} {self.target} {self.version}"]
        lines += [f"{k}: {v}" for k, v in self.headers]
        return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


class _HeadError(Exception):
    pass


async def _read_head(reader: asyncio.StreamReader) -> bytes:
    try:
        return await reader.readuntil(b"\r\n\r\n")
    except asyncio.LimitOverrunError:
        raise _HeadError("head too large") from None
    except asyncio.IncompleteReadError as exc:
        raise _HeadError("connection closed") from exc


def _parse_request(raw: bytes) -> _Request:
    text = raw.decode("latin-1")
    lines = text.split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) != 3 or not parts[2].startswith("HTTP/"):
        raise _HeadError("bad request line")
    headers: list[tuple[str, str]] = []
    for line in lines[1:]:
        if not line:
            continue
        name, sep, value = line.partition(":")
        if not sep or not name.strip() or name != name.strip():
            raise _HeadError("bad header")
        headers.append((name, value.strip()))
    return _Request(parts[0].upper(), parts[1], parts[2], headers)


def _split_host_port(target: str, default: int) -> tuple[str, int] | None:
    text = target.strip()
    if text.startswith("["):
        host, _, rest = text[1:].partition("]")
        port_text = rest.removeprefix(":")
    else:
        host, sep, port_text = text.rpartition(":")
        if not sep:
            host, port_text = text, ""
    try:
        port = int(port_text) if port_text else default
    except ValueError:
        return None
    if not 0 < port < 65536 or normalize_host(host) is None or host.startswith("*"):
        return None
    return host.lower(), port


async def _respond(writer: asyncio.StreamWriter, status: str, body: str = "") -> None:
    data = body.encode("utf-8")
    head = (
        f"HTTP/1.1 {status}\r\nContent-Type: text/plain; charset=utf-8\r\n"
        f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n"
    )
    with contextlib.suppress(ConnectionError, RuntimeError):
        writer.write(head.encode("latin-1") + data)
        await writer.drain()


async def _close(writer: asyncio.StreamWriter) -> None:
    with contextlib.suppress(Exception):
        writer.close()
        await asyncio.wait_for(writer.wait_closed(), 2.0)


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    with contextlib.suppress(
        ConnectionError, asyncio.IncompleteReadError, RuntimeError, ssl.SSLError
    ):
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    with contextlib.suppress(Exception):
        if writer.can_write_eof():
            writer.write_eof()


def _rewrite_response_head(raw: bytes) -> bytes:
    lines = raw.decode("latin-1").split("\r\n")
    kept = [lines[0]]
    kept += [
        ln for ln in lines[1:] if ln and ln.split(":", 1)[0].strip().lower() not in HOP_HEADERS
    ]
    kept.append("Connection: close")
    return ("\r\n".join(kept) + "\r\n\r\n").encode("latin-1")


class EgressProxy:
    """The forward proxy (see the module notes). ``secret`` reads a secret by name now;
    ``resolve`` and ``address_ok`` are the DNS and address-policy seams (tests use loopback)."""

    def __init__(
        self,
        *,
        policy: Callable[[], ProxyPolicy],
        secret: Callable[[str], str | None],
        tokens: TokenBook,
        authority: CertAuthority,
        upstream_tls: ssl.SSLContext | None = None,
        resolve: Resolve = system_resolve,
        address_ok: Callable[[str], bool] = public_only,
        instance: str = "",
    ) -> None:
        self.policy = policy
        self.secret = secret
        self.tokens = tokens
        self.authority = authority
        self.upstream_tls = upstream_tls or _default_upstream_tls()
        self.resolve = resolve
        self.address_ok = address_ok
        self.instance = instance or secrets.token_hex(8)
        self.server: asyncio.Server | None = None
        self.injected = 0

    async def start(self, host: str, port: int) -> tuple[str, int]:
        self.server = await asyncio.start_server(self._handle, host, port, limit=HEAD_LIMIT)
        bound = self.server.sockets[0].getsockname()
        return str(bound[0]), int(bound[1])

    async def aclose(self) -> None:
        if self.server is not None:
            self.server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.server.wait_closed(), 2.0)
            self.server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(_read_head(reader), HEAD_TIMEOUT_S)
            request = _parse_request(head)
        except (_HeadError, TimeoutError, ConnectionError):
            await _respond(writer, "400 Bad Request", "bad request\n")
            await _close(writer)
            return
        try:
            if request.method == "CONNECT":
                await self._connect(request, reader, writer)
            elif request.target.lower().startswith("http://"):
                await self._plain(request, reader, writer)
            elif request.method == "GET" and request.target == HEALTH_PATH:
                body = json.dumps({"instance": self.instance}) + "\n"
                await _respond(writer, "200 OK", body)
            else:
                await _respond(writer, "400 Bad Request", "use this address as an HTTP proxy\n")
        except Exception as exc:
            log.debug("egress: connection failed (%s)", type(exc).__name__)
        finally:
            await _close(writer)

    async def _dial(
        self, host: str, port: int, *, tls: bool
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter] | str:
        try:
            addresses = await asyncio.wait_for(self.resolve(host, port), CONNECT_TIMEOUT_S)
        except (OSError, TimeoutError):
            return "502 Bad Gateway"
        if not addresses or not all(self.address_ok(a) for a in addresses):
            log.info("egress: refused %s (not a public address)", host)
            return "403 Forbidden"
        try:
            return await asyncio.wait_for(
                asyncio.open_connection(
                    addresses[0],
                    port,
                    ssl=self.upstream_tls if tls else None,
                    server_hostname=host if tls else None,
                    limit=HEAD_LIMIT,
                ),
                CONNECT_TIMEOUT_S,
            )
        except (OSError, TimeoutError, ssl.SSLError):
            return "502 Bad Gateway"

    async def _connect(
        self, request: _Request, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        target = _split_host_port(request.target, 443)
        if target is None:
            await _respond(writer, "400 Bad Request", "bad CONNECT target\n")
            return
        host, port = target
        policy = self.policy()
        if host_allowed(host, policy.rules, policy.hosts):
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            await writer.start_tls(self.authority.server_context(host))
            await self._intercepted(host, port, policy, reader, writer)
            return
        if policy.block_other_hosts:
            log.info("egress: blocked CONNECT to %s", host)
            await _respond(writer, "403 Forbidden", "this host is not on the egress list\n")
            return
        upstream = await self._dial(host, port, tls=False)
        if isinstance(upstream, str):
            await _respond(writer, upstream, "")
            return
        up_reader, up_writer = upstream
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        try:
            await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))
        finally:
            await _close(up_writer)

    def _swap(self, text: str, value: str) -> tuple[str, int]:
        count = 0

        def one(match: re.Match[str]) -> str:
            nonlocal count
            if self.tokens.known(match.group(0)):
                count += 1
                return value
            return match.group(0)

        return TOKEN_RE.sub(one, text), count

    def _inject(self, host: str, request: _Request, policy: ProxyPolicy) -> list[str]:
        """Swap registered tokens for secrets; the names of the secrets used (never values)."""
        used: list[str] = []
        for rule in policy.rules:
            if not rule.secret or not _host_matches(rule.host, host):
                continue
            wanted = {h.lower() for h in rule.headers}
            if not any(TOKEN_RE.search(v) for k, v in request.headers if k.lower() in wanted) and (
                not TOKEN_RE.search(request.target)
            ):
                continue
            value = self.secret(rule.secret)
            if not value:
                continue
            swapped = 0
            for index, (name, header_value) in enumerate(request.headers):
                if name.lower() in wanted:
                    new, count = self._swap(header_value, value)
                    if count:
                        request.headers[index] = (name, new)
                        swapped += count
            new_target, count = self._swap(request.target, value)
            request.target = new_target
            swapped += count
            if swapped:
                used.append(rule.secret)
        return used

    async def _intercepted(
        self,
        host: str,
        port: int,
        policy: ProxyPolicy,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            head = await asyncio.wait_for(_read_head(reader), HEAD_TIMEOUT_S)
            request = _parse_request(head)
        except (_HeadError, TimeoutError, ConnectionError, ssl.SSLError):
            return
        used = self._inject(host, request, policy)
        request.headers = [(k, v) for k, v in request.headers if k.lower() not in HOP_HEADERS]
        request.headers.append(("Connection", "close"))
        upstream = await self._dial(host, port, tls=True)
        if isinstance(upstream, str):
            await _respond(writer, upstream, "")
            return
        if used:
            self.injected += 1
        log.info(
            "egress: %s %s%s",
            request.method,
            host,
            f" (injected {', '.join(used)})" if used else "",
        )
        up_reader, up_writer = upstream
        try:
            up_writer.write(request.encode())
            await up_writer.drain()
            sender = asyncio.ensure_future(_pipe(reader, up_writer))
            try:
                response_head = await asyncio.wait_for(_read_head(up_reader), 120.0)
                writer.write(_rewrite_response_head(response_head))
                await writer.drain()
                await _pipe(up_reader, writer)
            except (_HeadError, TimeoutError):
                await _respond(writer, "502 Bad Gateway", "")
            finally:
                sender.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await sender
        finally:
            await _close(up_writer)

    async def _plain(
        self, request: _Request, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        parts = urlsplit(request.target)
        host = (parts.hostname or "").lower()
        port = parts.port or 80
        policy = self.policy()
        if not host or normalize_host(host) is None:
            await _respond(writer, "400 Bad Request", "bad target\n")
            return
        if policy.block_other_hosts and not host_allowed(host, policy.rules, policy.hosts):
            await _respond(writer, "403 Forbidden", "this host is not on the egress list\n")
            return
        upstream = await self._dial(host, port, tls=False)
        if isinstance(upstream, str):
            await _respond(writer, upstream, "")
            return
        up_reader, up_writer = upstream
        path = parts.path or "/"
        request.target = path + (f"?{parts.query}" if parts.query else "")
        request.headers = [(k, v) for k, v in request.headers if k.lower() not in HOP_HEADERS]
        request.headers.append(("Connection", "close"))
        try:
            up_writer.write(request.encode())
            await up_writer.drain()
            sender = asyncio.ensure_future(_pipe(reader, up_writer))
            try:
                await _pipe(up_reader, writer)
            finally:
                sender.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await sender
        finally:
            await _close(up_writer)


def _default_upstream_tls() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


# -- the daemon -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProxyRecord:
    pid: int
    host: str
    port: int
    instance: str
    started: str = ""


def _record_path(home: Path) -> Path:
    return home / STATE_DIR / RECORD_FILE


def read_record(home: Path) -> ProxyRecord | None:
    try:
        data = json.loads(_record_path(home).read_text(encoding="utf-8"))
        return ProxyRecord(
            int(data["pid"]),
            str(data["host"]),
            int(data["port"]),
            str(data["instance"]),
            str(data.get("started", "")),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _write_record(home: Path, record: ProxyRecord) -> None:
    _private_dir(home / STATE_DIR)
    atomic_write_json(
        _record_path(home),
        {
            "pid": record.pid,
            "host": record.host,
            "port": record.port,
            "instance": record.instance,
            "started": record.started,
        },
        mode=0o600,
    )


def health(host: str, port: int, timeout: float = 1.0) -> str | None:
    """The instance id the proxy at ``host:port`` answers with, or None."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(f"GET {HEALTH_PATH} HTTP/1.1\r\nHost: egress\r\n\r\n".encode("ascii"))
            data = b""
            while len(data) < 4096:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
    except OSError:
        return None
    _, _, body = data.partition(b"\r\n\r\n")
    try:
        found = json.loads(body.decode("utf-8"))
    except ValueError:
        return None
    if not isinstance(found, dict):
        return None
    instance = cast(dict[str, object], found).get("instance")
    return instance if isinstance(instance, str) else None


def running(home: Path) -> ProxyRecord | None:
    """The live proxy of ``home``: its record answers health with its own instance id."""
    record = read_record(home)
    if record is None:
        return None
    return record if health(record.host, record.port) == record.instance else None


def _pid_alive(pid: int) -> bool:
    """Signal 0 probes the pid; a pid owned by someone else still counts as alive."""
    with contextlib.suppress(PermissionError):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
    return True


def _is_our_daemon(pid: int, home: Path) -> bool:
    """A pid whose command line is our serve verb for this home (Linux; else unknown = False)."""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    parts = [p.decode("utf-8", "replace") for p in raw.split(b"\0") if p]
    return "egress" in parts and "serve" in parts and str(home) in parts


def bind_address(scope: Scope, es: EgressSettings, settings: DockerSettings | None = None) -> str:
    """Where the proxy listens: ``egress.bind``; ``auto`` = the Docker bridge gateway on Linux,
    loopback elsewhere. Never an unspecified address."""
    chosen = es.bind
    if chosen == "auto":
        chosen = (
            "127.0.0.1"
            if not sys.platform.startswith("linux")
            else _bridge_gateway(scope, settings)
        )
    try:
        ip = ipaddress.ip_address(chosen)
    except ValueError:
        raise EgressUnavailable(f"egress.bind {chosen!r} is not an IP address") from None
    if ip.is_unspecified:
        raise EgressUnavailable("egress.bind may not listen on every interface")
    return str(ip)


def _bridge_gateway(scope: Scope, settings: DockerSettings | None) -> str:
    from indus.backends.docker.env import client_env
    from indus.platform.locate import which

    binary = (settings.binary if settings is not None else "") or "docker"
    found = which(binary, path=scope.env.get("PATH") or None)
    if found is None:
        raise EgressUnavailable("no Docker client to ask for the bridge address; set egress.bind")
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv
            [
                found,
                "network",
                "inspect",
                "bridge",
                "--format",
                "{{range .IPAM.Config}}{{.Gateway}} {{end}}",
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            env=client_env(scope),
            timeout=10.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise EgressUnavailable(
            "the Docker bridge address could not be read; set egress.bind"
        ) from None
    for word in done.stdout.decode("utf-8", "replace").split():
        with contextlib.suppress(ValueError):
            ip = ipaddress.ip_address(word)
            if ip.version == 4 and ip.is_private:
                return str(ip)
    raise EgressUnavailable("the Docker bridge has no gateway address; set egress.bind")


_CLOCK = SystemClock()  # the daemon's start/stop waits are real time
_children_lock = threading.Lock()
_children: dict[int, subprocess.Popen[bytes]] = {}


def start_daemon(
    scope: Scope,
    es: EgressSettings | None = None,
    *,
    bind: str | None = None,
    program: Sequence[str] | None = None,
    wait_s: float = START_WAIT_S,
) -> ProxyRecord:
    """The running proxy of this profile, started detached when there is none."""
    found = running(scope.home)
    if found is not None:
        return found
    settings = es or egress_settings(scope)
    host = bind or bind_address(scope, settings)
    stale = read_record(scope.home)
    if stale is not None and _pid_alive(stale.pid) and _is_our_daemon(stale.pid, scope.home):
        _terminate(stale.pid)
    _record_path(scope.home).unlink(missing_ok=True)
    folder = _private_dir(scope.home / STATE_DIR)
    argv = [
        *(program or (sys.executable, "-m", "indus")),
        "egress",
        "serve",
        "--home",
        str(scope.home),
        "--bind",
        host,
        "--port",
        str(settings.port),
    ]
    extra = {"INDUS_HOME": str(scope.home)}
    for name in ("DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR", "PYTHONPATH"):
        if scope.env.get(name):
            extra[name] = scope.env[name]
    env = child_env(scope, variant="minimal", extra=extra)
    with open(folder / LOG_FILE, "ab") as out:
        proc = subprocess.Popen(  # noqa: S603 - our own program
            argv,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=out,
            cwd=str(scope.home),
            env=env,
            start_new_session=True,
            close_fds=True,
        )
    with _children_lock:
        _children[proc.pid] = proc
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            _reap(proc.pid)
            raise EgressUnavailable(
                f"the egress proxy exited at start (code {proc.returncode})",
                hint=f"see {folder / LOG_FILE}",
            )
        record = read_record(scope.home)
        if (
            record is not None
            and record.pid == proc.pid
            and health(record.host, record.port) == record.instance
        ):
            return record
        _CLOCK.sleep_sync(0.05)
    _terminate(proc.pid)
    _reap(proc.pid)
    raise EgressUnavailable(
        "the egress proxy did not answer in time", hint=f"see {folder / LOG_FILE}"
    )


def ensure_running(scope: Scope, es: EgressSettings) -> ProxyRecord:
    return start_daemon(scope, es)


def _reap(pid: int) -> None:
    with _children_lock:
        proc = _children.pop(pid, None)
    if proc is not None:
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=STOP_WAIT_S)


def _terminate(pid: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + STOP_WAIT_S
    while time.monotonic() < deadline:
        with _children_lock:
            proc = _children.get(pid)
        if proc is not None:
            if proc.poll() is not None:
                break
        elif not _pid_alive(pid):
            break
        _CLOCK.sleep_sync(0.05)
    else:
        kill = getattr(signal, "SIGKILL", signal.SIGTERM)
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, kill)
    _reap(pid)


def stop_daemon(home: Path) -> bool:
    """Stop this profile's proxy; only a pid that proves to be ours is signalled."""
    record = read_record(home)
    if record is None:
        return False
    ours = health(record.host, record.port) == record.instance or _is_our_daemon(record.pid, home)
    stopped = False
    if ours and _pid_alive(record.pid):
        _terminate(record.pid)
        stopped = True
    _record_path(home).unlink(missing_ok=True)
    return stopped


# -- serving (the daemon's own side) --------------------------------------------------------------


class _LivePolicy:
    """The policy, re-read when the config file changes."""

    def __init__(self, scope: Scope, reload: Callable[[], Scope]) -> None:
        self.scope = scope
        self.reload = reload
        self.config_file = scope.home / "config.yaml"
        self._stamp: int | None = self._mtime()
        self._policy = self._build(scope)

    def _mtime(self) -> int | None:
        try:
            return self.config_file.stat().st_mtime_ns
        except OSError:
            return None

    @staticmethod
    def _build(scope: Scope) -> ProxyPolicy:
        es = egress_settings(scope)
        return ProxyPolicy(tuple(active_rules(scope, es)), es.hosts, es.block_other_hosts)

    def __call__(self) -> ProxyPolicy:
        stamp = self._mtime()
        if stamp != self._stamp:
            self._stamp = stamp
            with contextlib.suppress(Exception):
                self.scope = self.reload()
                self._policy = self._build(self.scope)
        return self._policy


async def serve(
    scope: Scope,
    *,
    bind: str,
    port: int,
    reload: Callable[[], Scope],
    secret: Callable[[str], str | None],
    stop: asyncio.Event | None = None,
) -> None:
    """Run the proxy for ``scope`` until ``stop`` is set or SIGTERM/SIGINT arrives."""
    folder = _private_dir(scope.home / STATE_DIR)
    authority = CertAuthority(folder).ensure()
    proxy = EgressProxy(
        policy=_LivePolicy(scope, reload),
        secret=secret,
        tokens=TokenBook(folder),
        authority=authority,
    )
    host, bound = await proxy.start(bind, port)
    record = ProxyRecord(os.getpid(), host, bound, proxy.instance, datetime.now(UTC).isoformat())
    _write_record(scope.home, record)
    log.info("egress proxy listening on %s:%d", host, bound)
    done = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    for signame in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, signame, None)
        if sig is not None:
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(sig, done.set)
    try:
        await done.wait()
    finally:
        await proxy.aclose()
        current = read_record(scope.home)
        if current is not None and current.instance == proxy.instance:
            _record_path(scope.home).unlink(missing_ok=True)
