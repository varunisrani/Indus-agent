"""Typed errors with stable codes.

Library code raises an ``IndusError`` subclass; boundaries (CLI, protocol servers) turn it into an
exit code or a protocol error. ``code`` is a stable machine string listed in ``ERROR_CODES``;
``message`` is what a person sees; ``hint`` is an optional next step; ``detail`` is for logs only
and never part of ``str(err)``.

This module imports only the standard library: ``foundation.preparse`` loads it before any app
module.
"""

from __future__ import annotations

from typing import ClassVar

# Every code the product raises, with a one-line meaning. Later generated into the protocol error
# table (D-16.27); a new code is added here first.
ERROR_CODES: dict[str, str] = {
    "indus.error": "unclassified product error",
    "home.unsafe_dir": "a resolved home or its parent is a filesystem root or top-level directory",
    "config.error": "configuration problem",
    "config.unreadable": "config file could not be read or parsed; nothing was written",
    "config.failed_read": "the loaded config is a fallback after a failed read and cannot be saved",
    "config.managed": "this install is managed by a package manager; config is read-only",
    "profile.error": "profile problem",
    "profile.not_found": "the named profile does not exist or was deleted",
    "profile.invalid_id": "the text is not a valid profile id",
    "profile.tombstoned": "the profile was deleted and cannot be recreated implicitly",
    "profile.sticky_missing": "the saved active profile no longer exists",
    "scope.unresolved": "no profile scope is set for this task or thread",
    "secrets.error": "secret store problem",
    "secrets.unavailable": "the OS keychain was required but cannot be used",
    "secrets.backend_failed": "the secret store backend failed to read, write or delete a value",
    "secrets.unreadable": "a secret store file cannot be read or parsed; it was left as it is",
    "secrets.bad_mode": "security.secret_store is not one of auto, keychain or file",
    "store.error": "session store problem",
    "provider.error": "model provider problem",
}


class IndusError(Exception):
    """Base of every product error.

    ``IndusError(code, message)`` names the code explicitly; subclasses carry a default code, so
    ``ProfileNotFound("message")`` is enough.
    """

    default_code: ClassVar[str] = "indus.error"

    def __init__(
        self,
        code_or_message: str,
        message: str | None = None,
        *,
        detail: object = None,
        hint: str | None = None,
    ) -> None:
        if message is None:
            code, text = self.default_code, code_or_message
        else:
            code, text = code_or_message, message
        super().__init__(text)
        self.code: str = code
        self.message: str = text
        self.detail: object = detail
        self.hint: str | None = hint

    def __str__(self) -> str:
        return self.message

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


class UnsafeHomeDir(IndusError):
    default_code = "home.unsafe_dir"


class ConfigError(IndusError):
    default_code = "config.error"


class ConfigUnreadable(ConfigError):
    default_code = "config.unreadable"


class ConfigFailedRead(ConfigError):
    default_code = "config.failed_read"


class ConfigManaged(ConfigError):
    default_code = "config.managed"


class ProfileError(IndusError):
    default_code = "profile.error"


class ProfileNotFound(ProfileError):
    default_code = "profile.not_found"


class InvalidProfileId(ProfileError):
    default_code = "profile.invalid_id"


class ProfileTombstoned(ProfileError):
    default_code = "profile.tombstoned"


class StickyProfileMissing(ProfileError):
    default_code = "profile.sticky_missing"


class ScopeUnresolved(IndusError):
    default_code = "scope.unresolved"


class SecretStoreError(IndusError):
    default_code = "secrets.error"


class SecretStoreUnavailable(SecretStoreError):
    """``security.secret_store: keychain`` was asked for but the keychain is not usable."""

    default_code = "secrets.unavailable"


class StoreError(IndusError):
    """Session store errors; P02 adds the subclasses."""

    default_code = "store.error"


class ProviderError(IndusError):
    """Model provider errors; P03 adds the subclasses."""

    default_code = "provider.error"
