# pyright: strict
"""The URL gate every fetching tool passes through (SSRF policy) and the user's website blocklist.

``check_url(url, scope)`` answers ``Allowed`` or ``Blocked(kind, reason)``. In order:

1. the text must parse as an ``http``/``https`` URL with a host;
2. a URL that carries a credential is refused before anything is fetched: a query parameter with a
   secret-like name and a value, or a vendor-key-shaped value anywhere in the URL;
3. the website blocklist (``security.website_blocklist``) is applied to the host;
4. cloud metadata host names are refused outright;
5. the host is resolved (a literal address needs no lookup) and **every** answer is classified.
   The metadata floor (well-known metadata addresses and all of ``169.254.0.0/16``) is refused even
   with ``security.allow_private_urls: true``; private, loopback, link-local, CGNAT, unique-local,
   multicast, reserved and unspecified answers are refused unless the user opted in, or the answer
   lies in a block a local TUN proxy was declared to answer with (``security.fake_ip_ranges``).
   IPv6 wrappers around an IPv4 address (mapped, translated, compatible, NAT64) are classified by
   the address they carry.

A lookup failure is refused, except when a proxy is configured: then the proxy resolves the name
and only the host-name rules above apply (a literal address is never deferred). Anything unexpected
is refused (fail closed). Config and proxy settings come from the call's ``Scope`` every time, so
one profile's opt-in never applies to another (R02.8).

The check is a snapshot: a name may resolve elsewhere a moment later (DNS rebinding). Fetching code
therefore uses ``indus.safety.safe_transport``, which runs this check again for every request it
sends (each redirect hop too) and dials one of the vetted addresses itself.
"""

from __future__ import annotations

import fnmatch
import functools
import ipaddress
import logging
import re
import socket
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Literal, cast
from urllib.parse import SplitResult, parse_qsl, quote, unquote, urlsplit, urlunsplit

from indus.foundation.blocking import run_blocking
from indus.foundation.config.schema import default_for
from indus.foundation.redact import redact_text
from indus.foundation.scope import Scope

__all__ = [
    "BLOCKLIST_TTL_S",
    "MAX_DIAL_ADDRESSES",
    "Allowed",
    "BlockHit",
    "BlockKind",
    "Blocked",
    "Resolver",
    "Verdict",
    "blocklist_hit",
    "check_url",
    "check_url_async",
    "classify_address",
    "clear_blocklist_cache",
    "declared_fake_ranges",
    "normalize_url",
    "proxy_for",
    "system_resolver",
]

log = logging.getLogger(__name__)

type IPAddr = ipaddress.IPv4Address | ipaddress.IPv6Address
type IPNet = ipaddress.IPv4Network | ipaddress.IPv6Network
type Resolver = Callable[[str, int | None], Sequence[str]]
"""``(host, port) -> address strings``; raises ``OSError`` when the name does not resolve."""

type BlockKind = Literal["invalid", "scheme", "secret", "blocklist", "metadata", "private", "dns"]

HTTP_SCHEMES = frozenset({"http", "https"})
MAX_DIAL_ADDRESSES = 8
BLOCKLIST_TTL_S = 30.0
PROXY_VARS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")

# -- verdicts ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Allowed:
    """The URL may be fetched. ``addresses`` are the vetted answers to dial (at most eight);
    empty when the name is left to a proxy (``via_proxy``)."""

    url: str
    host: str
    addresses: tuple[str, ...] = ()
    via_proxy: bool = False
    allowed: ClassVar[bool] = True


@dataclass(frozen=True)
class Blocked:
    """The URL must not be fetched; ``kind`` says which rule refused it."""

    url: str
    kind: BlockKind
    reason: str
    allowed: ClassVar[bool] = False


type Verdict = Allowed | Blocked

# -- address classes --------------------------------------------------------------------------

METADATA_HOSTS = frozenset(
    {"metadata.google.internal", "metadata.goog", "instance-data", "instance-data.ec2.internal"}
)
_METADATA_ADDRESSES: frozenset[IPAddr] = frozenset(
    ipaddress.ip_address(a)
    for a in (
        "169.254.169.254",  # the common instance metadata address
        "169.254.170.2",  # container task credentials
        "169.254.169.253",  # a cloud's DNS / wire server
        "100.100.100.200",  # a cloud's metadata address outside link-local
        "fd00:ec2::254",  # instance metadata over IPv6
    )
)
_FLOOR_NETWORKS: tuple[IPNet, ...] = (ipaddress.ip_network("169.254.0.0/16"),)
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_TRANSLATED = ipaddress.ip_network("::ffff:0:0:0/96")  # ::ffff:0:a.b.c.d
_COMPATIBLE = ipaddress.ip_network("::/96")  # ::a.b.c.d (deprecated form)
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
# Real internal space that no TUN proxy hands out as its sentinel answers. A declared fake-ip block
# opens whatever it covers, so an entry touching any of these is refused instead of honoured.
_UNDECLARABLE: tuple[IPNet, ...] = tuple(
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "100.64.0.0/10",
        "::/128",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
    )
)


def _parse_address(text: str) -> IPAddr | None:
    try:
        return ipaddress.ip_address(text.split("%", 1)[0].strip("[]"))
    except ValueError:
        return None


def _unwrap(ip: IPAddr) -> IPAddr:
    """The IPv4 address an IPv6 wrapper carries; ``ip`` itself otherwise."""
    if isinstance(ip, ipaddress.IPv4Address):
        return ip
    mapped = ip.ipv4_mapped
    if mapped is not None:
        return mapped
    if ip in _TRANSLATED or ip in _NAT64 or (ip in _COMPATIBLE and int(ip) > 1):
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return ip


def _in_floor(ip: IPAddr) -> bool:
    inner = _unwrap(ip)
    return (
        ip in _METADATA_ADDRESSES
        or inner in _METADATA_ADDRESSES
        or any(inner in net for net in _FLOOR_NETWORKS if net.version == inner.version)
    )


def _non_public(ip: IPAddr) -> bool:
    inner = _unwrap(ip)
    if isinstance(inner, ipaddress.IPv4Address) and inner in _CGNAT:
        return True
    if isinstance(inner, ipaddress.IPv6Address) and inner.is_site_local:
        return True
    return (
        inner.is_private
        or inner.is_loopback
        or inner.is_link_local
        or inner.is_reserved
        or inner.is_multicast
        or inner.is_unspecified
    )


def classify_address(
    ip: IPAddr, *, allow_private: bool = False, fake_ranges: Sequence[IPNet] = ()
) -> BlockKind | None:
    """``"metadata"`` or ``"private"`` when ``ip`` must not be dialled, else ``None``."""
    if _in_floor(ip):
        return "metadata"
    if allow_private or not _non_public(ip):
        return None
    inner = _unwrap(ip)
    if any(inner in net for net in fake_ranges if net.version == inner.version):
        return None
    return "private"


# -- settings from the scope ------------------------------------------------------------------


def _setting(scope: Scope | None, key: str) -> object:
    value: object = None
    if scope is not None and scope.config is not None:
        try:
            value = scope.config.get(key)
        except Exception:
            log.debug("config %s unreadable; using the default", key, exc_info=True)
    return default_for(key) if value is None else value


def _flag(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def declared_fake_ranges(scope: Scope | None) -> tuple[IPNet, ...]:
    """The usable entries of ``security.fake_ip_ranges``; bad or over-wide entries are dropped."""
    raw = _setting(scope, "security.fake_ip_ranges")
    entries: Iterable[object] = (
        [raw]
        if isinstance(raw, str)
        else cast(Iterable[object], raw)
        if isinstance(raw, list)
        else ()
    )
    kept: list[IPNet] = []
    for entry in entries:
        try:
            net = ipaddress.ip_network(str(entry).strip(), strict=False)
        except ValueError:
            log.warning("security.fake_ip_ranges: %r is not a network; ignored", entry)
            continue
        clash = next(
            (r for r in _UNDECLARABLE if r.version == net.version and net.overlaps(r)), None
        )
        if clash is not None:
            log.warning(
                "security.fake_ip_ranges: %s overlaps %s, which can never be declared; ignored",
                net,
                clash,
            )
            continue
        kept.append(net)
    return tuple(kept)


def proxy_for(scope: Scope | None, scheme: str = "https") -> str | None:
    """The proxy URL the scope's environment names for ``scheme`` (``ALL_PROXY`` as fallback)."""
    if scope is None:
        return None
    wanted = ("HTTPS_PROXY", "https_proxy") if scheme == "https" else ("HTTP_PROXY", "http_proxy")
    for name in (*wanted, "ALL_PROXY", "all_proxy"):
        value = scope.env.get(name)
        if value and value.strip():
            return value.strip()
    return None


# -- URL text ---------------------------------------------------------------------------------

_SPACE_AFTER_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*://)\s+")
_URL_SAFE = "/%:@!$&'()*+,;=~"


def _ascii_host(host: str) -> str:
    if host.startswith("["):
        return host
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return host


def normalize_url(text: str) -> str:
    """``text`` as an ASCII-safe http(s) URL: trimmed, no space after ``://``, the host IDNA-encoded
    and non-ASCII path/query/fragment text percent-encoded (existing escapes kept). Other text is
    returned trimmed and otherwise untouched."""
    raw = _SPACE_AFTER_SCHEME.sub(r"\1", text.strip())
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw
    if parts.scheme.lower() not in HTTP_SCHEMES:
        return raw
    userinfo, at, hostport = parts.netloc.rpartition("@")
    if hostport.startswith("["):
        host, port = hostport, ""
        close = hostport.find("]")
        if close != -1:
            host, port = hostport[: close + 1], hostport[close + 1 :]
    else:
        host, colon, port_digits = hostport.partition(":")
        port = colon + port_digits
    netloc = f"{userinfo}{at}{_ascii_host(host)}{port}"
    return urlunsplit(
        (
            parts.scheme,
            netloc,
            quote(parts.path, safe=_URL_SAFE),
            quote(parts.query, safe=_URL_SAFE + "?"),
            quote(parts.fragment, safe=_URL_SAFE + "?#"),
        )
    )


def _host_of(text: str) -> str:
    try:
        host = urlsplit(text).hostname or ""
    except ValueError:
        return ""
    return host.strip().lower().rstrip(".")


# Query parameter names that only ever carry a credential (ordinary page facets such as ``key``,
# ``code`` or ``sig`` are left out on purpose).
_SECRET_PARAMS = frozenset(
    {
        "access_token",
        "refresh_token",
        "id_token",
        "auth_token",
        "session_token",
        "private_token",
        "api_key",
        "apikey",
        "client_secret",
        "secret",
        "token",
        "password",
        "passwd",
        "credential",
        "credentials",
        "jwt",
        "signature",
        "authorization",
        "awsaccesskeyid",
        "x_amz_signature",
        "x_amz_security_token",
        "x_amz_credential",
    }
)


def _secret_in(parts: SplitResult) -> str | None:
    """A short description of the credential the URL carries, or ``None``."""
    for name, value in parse_qsl(parts.query, keep_blank_values=True):
        key = unquote(name).strip().lower().replace("-", "_")
        if value.strip() and key in _SECRET_PARAMS:
            return f"query parameter {name!r} looks like a credential"
    pieces = [
        f"{parts.scheme}://{parts.netloc}",
        unquote(parts.path),
        unquote(parts.fragment),
        *(unquote(v) for _, v in parse_qsl(parts.query, keep_blank_values=True)),
    ]
    for piece in pieces:
        if piece and redact_text(piece) != piece:
            return "the URL contains a credential-shaped value"
    return None


# -- resolving --------------------------------------------------------------------------------


def system_resolver(host: str, port: int | None) -> list[str]:
    """Every address the system resolver gives for ``host`` (TCP), as strings."""
    answers = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
    return [str(info[4][0]) for info in answers]


def _blocked(url: str, kind: BlockKind, reason: str) -> Blocked:
    log.warning("URL refused (%s): %s", kind, reason)
    return Blocked(url, kind, reason)


def check_url(url: str, scope: Scope | None, *, resolver: Resolver | None = None) -> Verdict:
    """The policy verdict for fetching ``url`` on behalf of ``scope`` (see the module notes)."""
    try:
        return _check(url, scope, resolver or system_resolver)
    except Exception as exc:  # fail closed: a parsing corner must never become a way through
        log.warning("URL check failed for %r: %s", url, exc)
        return Blocked(url, "invalid", f"the URL could not be checked: {type(exc).__name__}")


async def check_url_async(
    url: str, scope: Scope | None, *, resolver: Resolver | None = None
) -> Verdict:
    """``check_url`` with the lookup off the event loop (the ``io`` pool)."""
    work = functools.partial(check_url, url, scope, resolver=resolver)
    return await run_blocking(work, pool="io", label="url check")


def _check(url: str, scope: Scope | None, resolver: Resolver) -> Verdict:
    text = url.strip() if isinstance(url, str) else ""  # pyright: ignore[reportUnnecessaryIsInstance]
    if not text:
        return _blocked(url, "invalid", "the URL is empty")
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError as exc:
        return _blocked(url, "invalid", f"the URL does not parse: {exc}")
    scheme = parts.scheme.lower()
    if scheme not in HTTP_SCHEMES:
        shown = scheme or "none"
        return _blocked(url, "scheme", f"only http and https URLs are fetched (scheme: {shown})")
    host = (parts.hostname or "").strip().lower().rstrip(".")
    if not host:
        return _blocked(url, "invalid", "the URL has no host")
    secret = _secret_in(parts)
    if secret is not None:
        return _blocked(url, "secret", f"{secret}; secrets must not be sent in URLs")
    hit = blocklist_hit(text, scope)
    if hit is not None:
        return _blocked(url, "blocklist", hit.message)
    if host in METADATA_HOSTS:
        return _blocked(url, "metadata", f"{host} is a cloud metadata host")

    allow_private = _flag(_setting(scope, "security.allow_private_urls"))
    fake = declared_fake_ranges(scope)
    literal = _parse_address(host)
    if literal is not None:
        kind = classify_address(literal, allow_private=allow_private, fake_ranges=fake)
        if kind is not None:
            return _blocked(url, kind, _class_reason(kind, host, str(literal)))
        return Allowed(text, host, (str(literal),))

    proxied = proxy_for(scope, scheme) is not None
    try:
        answers = list(resolver(host, port))
    except OSError as exc:
        if proxied:
            log.debug("lookup of %s failed; the configured proxy will resolve it", host)
            return Allowed(text, host, (), via_proxy=True)
        return _blocked(url, "dns", f"{host} did not resolve ({exc})")
    if not answers:
        if proxied:
            return Allowed(text, host, (), via_proxy=True)
        return _blocked(url, "dns", f"{host} resolved to no address")
    kept: list[str] = []
    for answer in answers:
        ip = _parse_address(answer)
        if ip is None:
            return _blocked(url, "invalid", f"{host} resolved to an unreadable address {answer!r}")
        kind = classify_address(ip, allow_private=allow_private, fake_ranges=fake)
        if kind is not None:
            return _blocked(url, kind, _class_reason(kind, host, str(ip)))
        if str(ip) not in kept and len(kept) < MAX_DIAL_ADDRESSES:
            kept.append(str(ip))
    return Allowed(text, host, tuple(kept))


def _class_reason(kind: BlockKind, host: str, address: str) -> str:
    where = host if host == address else f"{host} ({address})"
    if kind == "metadata":
        return f"{where} is a cloud metadata address"
    return f"{where} is a private or internal network address"


# -- website blocklist ------------------------------------------------------------------------


@dataclass(frozen=True)
class BlockRule:
    pattern: str
    source: str


@dataclass(frozen=True)
class BlockHit:
    host: str
    rule: str
    source: str

    @property
    def message(self) -> str:
        return f"{self.host} matches the website blocklist rule {self.rule!r} (from {self.source})"


@dataclass(frozen=True)
class _Policy:
    enabled: bool
    rules: tuple[BlockRule, ...]


_OFF = _Policy(False, ())


def _rule_of(text: object) -> str | None:
    if not isinstance(text, str):
        return None
    value = text.strip().lower()
    if not value or value.startswith("#"):
        return None
    if "://" in value:
        value = value.split("://", 1)[1]
    value = value.split("/", 1)[0].split("@")[-1]
    if not value.startswith("[") and value.count(":") == 1:
        value = value.split(":", 1)[0]
    value = value.strip().rstrip(".").removeprefix("www.")
    return value or None


def _file_rules(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        log.warning("website blocklist file %s not found; skipped", path)
        return []
    except (OSError, UnicodeDecodeError) as exc:
        log.warning("website blocklist file %s unreadable (%s); skipped", path, exc)
        return []
    return [rule for rule in map(_rule_of, text.splitlines()) if rule]


def _load_policy(scope: Scope) -> _Policy:
    raw = _setting(scope, "security.website_blocklist")
    if not isinstance(raw, Mapping):
        if raw is not None:
            log.warning("security.website_blocklist must be a mapping; blocklist off")
        return _OFF
    section = cast(Mapping[str, Any], raw)
    enabled = section.get("enabled", False)
    domains: object = section.get("domains") or []
    files: object = section.get("shared_files") or []
    if not isinstance(enabled, bool) or not isinstance(domains, list):
        log.warning("security.website_blocklist is malformed; blocklist off")
        return _OFF
    if not enabled:
        return _OFF
    pairs: list[tuple[str, str]] = [
        (rule, "config") for rule in map(_rule_of, cast(list[object], domains)) if rule
    ]
    for entry in cast(list[object], files) if isinstance(files, list) else []:
        if not isinstance(entry, str) or not entry.strip():
            continue
        path = Path(entry.strip()).expanduser()
        if not path.is_absolute():
            path = scope.home / path
        pairs.extend((rule, str(path)) for rule in _file_rules(path))
    return _Policy(True, tuple(BlockRule(p, s) for p, s in dict.fromkeys(pairs)))


class _PolicyCache:
    """Parsed blocklists per profile, each kept ``BLOCKLIST_TTL_S`` on that profile's clock."""

    def __init__(self) -> None:
        self._entries: dict[str, tuple[float, _Policy]] = {}
        self._lock = threading.Lock()

    def get(self, scope: Scope) -> _Policy:
        key = str(scope.home)
        now = scope.clock.monotonic()
        with self._lock:
            hit = self._entries.get(key)
            if hit is not None and now - hit[0] < BLOCKLIST_TTL_S:
                return hit[1]
        try:
            policy = _load_policy(scope)
        except Exception:
            log.warning("website blocklist could not be loaded; blocklist off", exc_info=True)
            policy = _OFF
        with self._lock:
            self._entries[key] = (now, policy)
        return policy

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


_cache = _PolicyCache()


def clear_blocklist_cache() -> None:
    _cache.clear()


def _matches(host: str, pattern: str) -> bool:
    if any(ch in pattern for ch in "*?["):
        return fnmatch.fnmatchcase(host, pattern)
    return host == pattern or host.endswith("." + pattern)


def blocklist_hit(url: str, scope: Scope | None) -> BlockHit | None:
    """The blocklist rule ``url``'s host matches, or ``None`` (also when the list is off)."""
    if scope is None:
        return None
    policy = _cache.get(scope)
    if not policy.enabled or not policy.rules:
        return None
    host = _host_of(url) or _host_of("//" + url)
    if not host:
        return None
    for rule in policy.rules:
        if _matches(host, rule.pattern):
            return BlockHit(host, rule.pattern, rule.source)
    return None
