"""AWS Signature Version 4 for our own HTTP requests, plus the credential and region chain.

The signing steps are AWS's published ones: a canonical request (method, canonical URI, sorted
canonical query, sorted lower-case headers with trimmed values, the signed-header list and the
SHA-256 of the body), a string to sign (``AWS4-HMAC-SHA256``, the timestamp, the credential scope
``date/region/service/aws4_request`` and the hash of the canonical request) and an HMAC chain
``AWS4<secret>`` → date → region → service → ``aws4_request``. Path segments are encoded once
more than they arrive (the rule for every service except S3, which we never call).

Credentials come from the scope's environment (``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY`` /
``AWS_SESSION_TOKEN``), then the shared credentials file (``AWS_SHARED_CREDENTIALS_FILE`` or
``~/.aws/credentials``, profile ``AWS_PROFILE`` or ``default``). SSO, assume-role and instance
metadata are not read here.
"""

from __future__ import annotations

import configparser
import hashlib
import hmac
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, unquote

import httpx

from indus.foundation.home import expand_path_text

log = logging.getLogger(__name__)

ALGORITHM = "AWS4-HMAC-SHA256"
TERMINATOR = "aws4_request"
DEFAULT_REGION = "us-east-1"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
ACCESS_KEY_ENV = "AWS_ACCESS_KEY_ID"
SECRET_KEY_ENV = "AWS_SECRET_ACCESS_KEY"
SESSION_TOKEN_ENV = "AWS_SESSION_TOKEN"
PROFILE_ENV = "AWS_PROFILE"
SHARED_FILE_ENV = "AWS_SHARED_CREDENTIALS_FILE"
REGION_ENVS = ("AWS_REGION", "AWS_DEFAULT_REGION")
DEFAULT_SHARED_FILE = "~/.aws/credentials"
# Headers that change between the signer and the wire, or that proxies rewrite: never signed.
_UNSIGNED = frozenset({"authorization", "user-agent", "content-length", "connection", "expect"})
_SPACES = re.compile(r"\s+")
_UNRESERVED = "-_.~"


@dataclass(frozen=True)
class AwsCredentials:
    access_key: str
    secret_key: str = field(repr=False)
    session_token: str | None = field(default=None, repr=False)
    source: str = ""


# -- credentials and region ---------------------------------------------------------------------


def _shared_file(env: Mapping[str, str], user_home: Callable[[], Path] | None) -> Path:
    raw = env.get(SHARED_FILE_ENV, "").strip() or DEFAULT_SHARED_FILE
    return expand_path_text(raw, env, user_home)


def _from_shared_file(
    env: Mapping[str, str], user_home: Callable[[], Path] | None
) -> AwsCredentials | None:
    path = _shared_file(env, user_home)
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        log.warning("could not read the AWS credentials file %s: %s", path, exc)
        return None
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(text)
    except configparser.Error as exc:
        log.warning("the AWS credentials file %s does not parse: %s", path, exc)
        return None
    profile = env.get(PROFILE_ENV, "").strip() or "default"
    if not parser.has_section(profile):
        return None
    section = parser[profile]
    access = section.get("aws_access_key_id", "").strip()
    secret = section.get("aws_secret_access_key", "").strip()
    if not access or not secret:
        return None
    token = section.get("aws_session_token", "").strip() or None
    return AwsCredentials(access, secret, token, source=f"shared file ({profile})")


def resolve_credentials(
    env: Mapping[str, str], *, user_home: Callable[[], Path] | None = None
) -> AwsCredentials | None:
    """The first credentials of the standard chain we support, or None."""
    access = env.get(ACCESS_KEY_ENV, "").strip()
    secret = env.get(SECRET_KEY_ENV, "").strip()
    if access and secret:
        token = env.get(SESSION_TOKEN_ENV, "").strip() or None
        return AwsCredentials(access, secret, token, source="environment")
    return _from_shared_file(env, user_home)


_REGION_HOST = re.compile(r"(?:^|\.)bedrock-runtime(?:-fips)?\.([a-z0-9-]+)\.amazonaws\.com$")


def region_from_host(host: str) -> str | None:
    """``us-west-2`` for ``bedrock-runtime.us-west-2.amazonaws.com``; None for other hosts."""
    match = _REGION_HOST.search(host.lower())
    return match.group(1) if match else None


def resolve_region(host: str, env: Mapping[str, str]) -> str:
    """The endpoint's own region first (a signature must match the host), then the env."""
    from_host = region_from_host(host)
    if from_host:
        return from_host
    for name in REGION_ENVS:
        value = env.get(name, "").strip()
        if value:
            return value
    return DEFAULT_REGION


# -- signing --------------------------------------------------------------------------------------


def _hmac(key: bytes, text: str) -> bytes:
    return hmac.new(key, text.encode("utf-8"), hashlib.sha256).digest()


def signing_key(secret_key: str, date: str, region: str, service: str) -> bytes:
    """The derived key for one day, region and service (``date`` as ``YYYYMMDD``)."""
    k_date = _hmac(f"AWS4{secret_key}".encode(), date)
    k_region = _hmac(k_date, region)
    k_service = _hmac(k_region, service)
    return _hmac(k_service, TERMINATOR)


def _encode(text: str) -> str:
    return quote(text, safe=_UNRESERVED)


def canonical_uri(path: str) -> str:
    """Each path segment URI-encoded once more than it was sent (non-S3 rule)."""
    if not path:
        return "/"
    return "/".join(_encode(segment) for segment in path.split("/"))


def canonical_query(query: str) -> str:
    """Parameters sorted by encoded name, then value; each side encoded per RFC 3986."""
    if not query:
        return ""
    pairs: list[tuple[str, str]] = []
    for part in query.split("&"):
        if not part:
            continue
        name, _, value = part.partition("=")
        pairs.append((_encode(unquote(name)), _encode(unquote(value))))
    return "&".join(f"{n}={v}" for n, v in sorted(pairs))


def _header_value(value: str) -> str:
    return _SPACES.sub(" ", value.strip())


def canonical_parts(request: httpx.Request, payload_hash: str) -> tuple[str, str]:
    """``(canonical request, signed headers)`` for ``request`` as it stands."""
    headers: dict[str, str] = {}
    for name, value in request.headers.multi_items():
        lowered = name.lower()
        if lowered in _UNSIGNED:
            continue
        if lowered in headers:
            headers[lowered] = f"{headers[lowered]},{_header_value(value)}"
        else:
            headers[lowered] = _header_value(value)
    names = sorted(headers)
    signed = ";".join(names)
    lines = "".join(f"{n}:{headers[n]}\n" for n in names)
    raw_path = request.url.raw_path.decode("ascii")
    path, _, query = raw_path.partition("?")
    canonical = "\n".join(
        [
            request.method.upper(),
            canonical_uri(path),
            canonical_query(query),
            lines,
            signed,
            payload_hash,
        ]
    )
    return canonical, signed


def sign(
    request: httpx.Request,
    credentials: AwsCredentials,
    *,
    region: str,
    service: str,
    now: datetime,
) -> httpx.Request:
    """Add ``x-amz-date`` (and the session token) and the ``Authorization`` header in place."""
    stamp = now.astimezone(UTC)
    amz_date = stamp.strftime("%Y%m%dT%H%M%SZ")
    day = stamp.strftime("%Y%m%d")
    request.headers["x-amz-date"] = amz_date
    if credentials.session_token:
        request.headers["x-amz-security-token"] = credentials.session_token
    if "host" not in request.headers:
        request.headers["host"] = request.url.netloc.decode("ascii")
    body = request.content
    payload_hash = hashlib.sha256(body).hexdigest() if body else EMPTY_SHA256
    canonical, signed = canonical_parts(request, payload_hash)
    scope = f"{day}/{region}/{service}/{TERMINATOR}"
    to_sign = "\n".join(
        [ALGORITHM, amz_date, scope, hashlib.sha256(canonical.encode("utf-8")).hexdigest()]
    )
    key = signing_key(credentials.secret_key, day, region, service)
    signature = hmac.new(key, to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    request.headers["authorization"] = (
        f"{ALGORITHM} Credential={credentials.access_key}/{scope}, "
        f"SignedHeaders={signed}, Signature={signature}"
    )
    return request
