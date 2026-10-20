"""The username/password provider (P18-03, D-16.44).

One shared credential for a dashboard on a trusted network. The password is stored as an argon2id
PHC string (``$argon2id$v=19$m=…,t=…,p=…$salt$hash``) in ``dashboard.basic_auth.password_hash``,
computed and checked by ``cryptography``'s ``Argon2id`` — no hashing code of our own. A plaintext
``INDUS_DASHBOARD_PASSWORD`` in the environment wins over the stored hash, so the password can be
rotated without editing the config; it is compared through SHA-256 digests in constant time.
``INDUS_DASHBOARD_USERNAME`` likewise wins over ``dashboard.basic_auth.username``.

A login checks the username in constant time and always runs one password verification — against a
fixed dummy hash when the username is wrong — so an unknown user and a wrong password take the same
path and get the same answer (``BadCredentials``). A good login is a session from the shared signer
(TTL 43,200 s). Verifying a session also checks that its user is still the configured one, so
changing the username ends existing sessions.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Callable, Mapping
from typing import Any, ClassVar, Final

from cryptography.exceptions import InvalidKey
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id

from indus.web.auth.providers import (
    BadCredentials,
    Provider,
    ProviderRegistry,
    ProviderUnavailable,
    Session,
    SessionSigner,
)

__all__ = [
    "PASSWORD_ENV",
    "USERNAME_ENV",
    "BasicProvider",
    "hash_password",
    "looks_like_hash",
    "register_basic",
    "verify_password",
]

USERNAME_ENV: Final = "INDUS_DASHBOARD_USERNAME"
PASSWORD_ENV: Final = "INDUS_DASHBOARD_PASSWORD"
HASH_PREFIX: Final = "$argon2id$"
MEMORY_KIB: Final = 64 * 1024
ITERATIONS: Final = 3
LANES: Final = 4
SALT_BYTES: Final = 16
HASH_BYTES: Final = 32
MAX_PASSWORD_BYTES: Final = 4096


def hash_password(
    password: str, *, memory_kib: int = MEMORY_KIB, iterations: int = ITERATIONS, lanes: int = LANES
) -> str:
    """An argon2id PHC string for ``password`` with a fresh random salt."""
    if not password:
        raise ValueError("the password must not be empty")
    kdf = Argon2id(
        salt=secrets.token_bytes(SALT_BYTES),
        length=HASH_BYTES,
        iterations=iterations,
        lanes=lanes,
        memory_cost=memory_kib,
    )
    return kdf.derive_phc_encoded(password.encode("utf-8"))


def looks_like_hash(value: object) -> bool:
    return isinstance(value, str) and value.startswith(HASH_PREFIX) and value.count("$") == 5


def verify_password(password: str, encoded: str) -> bool:
    """True when ``password`` matches the argon2id PHC string ``encoded``."""
    if not looks_like_hash(encoded):
        return False
    data = password.encode("utf-8", errors="surrogatepass")
    if len(data) > MAX_PASSWORD_BYTES:
        return False
    try:
        Argon2id.verify_phc_encoded(data, encoded)
    except (InvalidKey, ValueError, TypeError):
        return False
    return True


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).digest()


_dummy_hash: str | None = None


def _dummy() -> str:
    """A fixed hash to verify against when the username is wrong (same cost as a real check)."""
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_password(secrets.token_urlsafe(16))
    return _dummy_hash


class BasicProvider(Provider):
    name: ClassVar[str] = "basic"
    display_name: ClassVar[str] = "Username and password"
    kind = "password"

    def __init__(
        self,
        *,
        username: str,
        signer: SessionSigner,
        password_hash: str = "",
        plaintext: str = "",
        dummy_hash: Callable[[], str] = _dummy,
    ) -> None:
        if not username:
            raise ValueError("a username is required")
        if not plaintext and not looks_like_hash(password_hash):
            raise ValueError("an argon2id password hash or a plaintext password is required")
        self._username_digest = _digest(username)
        self._username = username
        self._hash = password_hash
        self._plain_digest = _digest(plaintext) if plaintext else b""
        self._signer = signer
        self._dummy = dummy_hash

    def _password_ok(self, password: str) -> bool:
        if self._plain_digest:
            return hmac.compare_digest(_digest(password), self._plain_digest)
        return verify_password(password, self._hash)

    def password_login(self, username: str, password: str) -> Session:
        user_ok = hmac.compare_digest(_digest(username), self._username_digest)
        if user_ok:
            password_ok = self._password_ok(password)
        else:
            if self._plain_digest:
                hmac.compare_digest(_digest(password), self._plain_digest)
            else:
                verify_password(password, self._dummy())
            password_ok = False
        if not (user_ok and password_ok):
            raise BadCredentials("wrong username or password")
        return self._signer.mint(
            Session(
                user_id=self._username,
                provider=self.name,
                expires_at=0,
                display_name=self._username,
            )
        )

    def verify_session(self, token: str) -> Session | None:
        session = self._signer.verify(token, provider=self.name)
        if session is None:
            return None
        if not hmac.compare_digest(_digest(session.user_id), self._username_digest):
            return None
        return session


def register_basic(
    registry: ProviderRegistry,
    settings: Mapping[str, Any],
    env: Mapping[str, str],
    signer: SessionSigner,
) -> BasicProvider | None:
    """Register the provider when a username and a password are configured, else record why not.
    ``settings`` is the ``dashboard.basic_auth`` mapping."""
    username = env.get(USERNAME_ENV, "").strip() or str(settings.get("username") or "").strip()
    plaintext = env.get(PASSWORD_ENV, "")
    stored = str(settings.get("password_hash") or "").strip()
    if not username:
        registry.unavailable(BasicProvider.name, "no username set (dashboard.basic_auth.username)")
        return None
    if not plaintext and not stored:
        registry.unavailable(
            BasicProvider.name,
            f"no password set (dashboard.basic_auth.password_hash or {PASSWORD_ENV})",
        )
        return None
    if not plaintext and not looks_like_hash(stored):
        registry.unavailable(
            BasicProvider.name, "dashboard.basic_auth.password_hash is not an argon2id hash"
        )
        return None
    try:
        provider = BasicProvider(
            username=username, signer=signer, password_hash=stored, plaintext=plaintext
        )
    except (ValueError, ProviderUnavailable) as exc:
        registry.unavailable(BasicProvider.name, str(exc))
        return None
    registry.register(provider)
    return provider
