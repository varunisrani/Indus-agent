"""Base-URL parsing, exact host matching and API-mode detection from a URL.

Every decision about where a key may be sent is made on a parsed, normalised host and never on a
substring of the URL text: ``https://api.openai.com.evil.example`` and
``https://evil.example/api.openai.com`` must not pass for ``api.openai.com`` (R01.11).
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from urllib.parse import urlsplit

from indus.foundation.errors import InvalidBaseURL

SCHEMES = frozenset({"http", "https"})
DEFAULT_PORTS = {"http": 80, "https": 443}

# Hosts whose chat endpoint is best served by the Responses API (P08-09).
OPENAI_OFFICIAL_HOSTS = frozenset({"api.openai.com", "us.api.openai.com", "eu.api.openai.com"})
ANTHROPIC_OFFICIAL_HOSTS = frozenset({"api.anthropic.com"})

API_MODE_CHAT = "chat_completions"
API_MODE_ANTHROPIC = "anthropic_messages"
API_MODE_RESPONSES = "responses"


@dataclass(frozen=True)
class ParsedURL:
    scheme: str
    host: str
    port: int | None
    path: str
    query: str = ""

    @property
    def effective_port(self) -> int:
        return self.port if self.port is not None else DEFAULT_PORTS[self.scheme]

    @property
    def origin(self) -> tuple[str, str, int]:
        return (self.scheme, self.host, self.effective_port)


def _bad(url: str, why: str) -> InvalidBaseURL:
    return InvalidBaseURL(f"{url!r} is not a usable base URL: {why}")


def normalise_host(host: str) -> str:
    """Lowercase, drop one trailing dot and convert non-ASCII labels to punycode (IDNA)."""
    text = host.strip().lower()
    if text.endswith("."):
        text = text[:-1]
    if not text:
        raise ValueError("empty host")
    if ":" in text:  # IPv6 literal (brackets already removed by urlsplit)
        return str(ipaddress.IPv6Address(text.split("%", 1)[0]))
    if not text.isascii():
        text = text.encode("idna").decode("ascii")
    labels = text.split(".")
    if any(not label for label in labels):
        raise ValueError("empty label")
    for label in labels:
        if len(label) > 63 or not all(c.isalnum() or c in "-_" for c in label):
            raise ValueError(f"bad label {label!r}")
    return text


def parse_base_url(url: str) -> ParsedURL:
    """Parse a provider base URL strictly; raise ``InvalidBaseURL`` on anything ambiguous."""
    if not isinstance(url, str) or not url:  # pyright: ignore[reportUnnecessaryIsInstance]
        raise _bad(str(url), "empty")
    if any(ch.isspace() for ch in url):
        raise _bad(url, "contains whitespace")
    if "\\" in url:
        raise _bad(url, "contains a backslash")
    if "://" not in url:
        raise _bad(url, "no scheme (expected http:// or https://)")
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise _bad(url, str(exc)) from exc
    scheme = parts.scheme.lower()
    if scheme not in SCHEMES:
        raise _bad(url, f"scheme {parts.scheme!r} is not http or https")
    if "@" in parts.netloc:
        raise _bad(url, "user info before the host is not allowed")
    if parts.fragment or url.endswith("#"):
        raise _bad(url, "fragments are not allowed")
    raw_host = parts.hostname
    if not raw_host:
        raise _bad(url, "no host")
    try:
        port = parts.port
    except ValueError as exc:
        raise _bad(url, "bad port") from exc
    try:
        host = normalise_host(raw_host)
    except (UnicodeError, ValueError) as exc:
        raise _bad(url, f"bad host ({exc})") from exc
    return ParsedURL(
        scheme=scheme, host=host, port=port, path=parts.path.rstrip("/"), query=parts.query
    )


def try_parse(url: str) -> ParsedURL | None:
    try:
        return parse_base_url(url)
    except InvalidBaseURL:
        return None


def host_of(url: str) -> str:
    """The normalised host of ``url``, or ``""`` when it does not parse."""
    parsed = try_parse(url)
    return parsed.host if parsed else ""


def host_matches(url: str, domain: str, *, allow_subdomains: bool = False) -> bool:
    """True when ``url``'s host is exactly ``domain``.

    With ``allow_subdomains`` a host ending in ``"." + domain`` also matches; callers pass it only
    for a suffix they explicitly allow. Never raises: an unparsable URL or domain does not match.
    """
    parsed = try_parse(url)
    if parsed is None or not domain:
        return False
    try:
        wanted = normalise_host(domain)
    except (UnicodeError, ValueError):
        return False
    if parsed.host == wanted:
        return True
    return allow_subdomains and parsed.host.endswith("." + wanted)


def _ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def is_loopback(url: str) -> bool:
    """127.0.0.0/8, ``::1`` (and its IPv4-mapped form) or ``localhost``."""
    parsed = try_parse(url)
    if parsed is None:
        return False
    if parsed.host == "localhost":
        return True
    addr = _ip(parsed.host)
    return addr is not None and addr.is_loopback


def is_private_network(url: str) -> bool:
    """Loopback, RFC 1918, link-local or unique-local addresses (a server on the user's network)."""
    if is_loopback(url):
        return True
    parsed = try_parse(url)
    if parsed is None:
        return False
    addr = _ip(parsed.host)
    return addr is not None and (addr.is_private or addr.is_link_local) and not addr.is_unspecified


def api_mode_from_url(url: str) -> str:
    """The wire format a URL most likely speaks.

    Official OpenAI hosts → ``responses``;
    ``api.anthropic.com`` or a path ending ``/anthropic`` or ``/anthropic/v1`` →
    ``anthropic_messages``; everything else → ``chat_completions``. Raises ``InvalidBaseURL``.
    """
    parsed = parse_base_url(url)
    if parsed.host in OPENAI_OFFICIAL_HOSTS:
        return API_MODE_RESPONSES
    if parsed.host in ANTHROPIC_OFFICIAL_HOSTS:
        return API_MODE_ANTHROPIC
    path = parsed.path.lower()
    if path.endswith("/anthropic") or path.endswith("/anthropic/v1"):
        return API_MODE_ANTHROPIC
    return API_MODE_CHAT
