"""The frozen creation policy: how a daemon-hosted session was launched, fixed for life (P17-09).

When the daemon creates a session it records the launch choices that belong to that session and
not to the daemon: working directory, model, provider, reasoning effort, toolsets, turn cap,
preloaded skills, checkpoints, the safety switches (yolo, accept-hooks, ignore-rules, safe mode,
ignore-user-config), passing the session id to the model, the base URL and a *reference* to the
key to use. A resume may name the same
values again; naming a different one is refused with ``policy_conflict`` and the field (R04.1).

- **Secrets never freeze.** An ``api_key`` handed in with the launch is dropped (never stored, never
  compared). ``key_ref`` names where the key lives (an env var or secret name); a value that looks
  like a credential is refused, as is a base URL carrying user info.
- **Bypass launches.** Safe mode and ignore-user-config read no profile defaults, so such a launch
  must name both the model and the provider (``check_launch``).
- **Launch env vars** (``INDUS_YOLO_MODE`` and friends) are turned into creation fields on the
  client side (``launch_flags_from_env``) and removed from the environment used to reach or start
  the daemon (``scrub_launch_env``): they belong to one session, not to the daemon.

The record lives in the session row's ``model_config["launch"]``, the key the in-process CLI uses
for the same purpose, so both hosts check one record. ``None`` (or an empty list) means "the
configured default"; ``False`` for a switch means it was not asked for; ``max_turns=0`` is a value.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, fields
from typing import Any, ClassVar, Final, Literal, cast
from urllib.parse import urlsplit

from indus.foundation.errors import IndusError
from indus.foundation.redact import redact_text
from indus.state import sessions

__all__ = [
    "BYPASS_FIELDS",
    "FROZEN_FIELDS",
    "LAUNCH_ENV",
    "LAUNCH_KEY",
    "CreationPolicy",
    "PolicyConflict",
    "PolicyRefused",
    "check_launch",
    "check_resume",
    "launch_flags_from_env",
    "policy_patch",
    "require_same",
    "scrub_launch_env",
    "stored_policy",
]

LAUNCH_KEY: Final = "launch"
"""The ``model_config`` key holding the record (shared with ``cli.session_select``)."""

FROZEN_FIELDS: Final[tuple[str, ...]] = (
    "cwd",
    "model",
    "provider",
    "reasoning",
    "toolsets",
    "max_turns",
    "skills",
    "checkpoints",
    "yolo",
    "accept_hooks",
    "ignore_rules",
    "base_url",
    "key_ref",
    "safe_mode",
    "ignore_user_config",
    "pass_session_id",
)

BYPASS_FIELDS: Final[tuple[str, ...]] = ("safe_mode", "ignore_user_config")
"""Switches under which the launch reads no profile defaults."""

LAUNCH_ENV: Final[Mapping[str, str]] = {
    "INDUS_YOLO_MODE": "yolo",
    "INDUS_IGNORE_RULES": "ignore_rules",
    "INDUS_SAFE_MODE": "safe_mode",
    "INDUS_IGNORE_USER_CONFIG": "ignore_user_config",
    "INDUS_ACCEPT_HOOKS": "accept_hooks",
}
"""Launch-option env vars and the creation field each one sets."""

_DROPPED: Final = frozenset({"api_key"})
_SWITCHES: Final = frozenset(
    {
        "checkpoints",
        "yolo",
        "accept_hooks",
        "ignore_rules",
        "safe_mode",
        "ignore_user_config",
        "pass_session_id",
    }
)
_TEXTS: Final = frozenset({"cwd", "model", "provider", "reasoning", "base_url", "key_ref"})
_LISTS: Final = frozenset({"toolsets", "skills"})
_TRUTHY: Final = frozenset({"1", "true", "yes", "on"})
_REF: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_.:/-]{0,127}")
_MAX_TEXT: Final = 4096

type RefusalReason = Literal[
    "invalid_field", "invalid_value", "secret_in_policy", "bypass_needs_route"
]


class PolicyRefused(IndusError):
    """A launch policy that cannot be frozen as given (``reason`` names why, ``field`` where)."""

    default_code = "runtime.policy_refused"

    def __init__(self, reason: RefusalReason, field: str) -> None:
        self.reason: RefusalReason = reason
        self.field = field
        super().__init__(self.default_code, f"{reason} ({field})")


class PolicyConflict(IndusError):
    """A resume named launch values that differ from the session's frozen ones."""

    default_code = "runtime.policy_conflict"
    code_name: ClassVar[str] = "policy_conflict"

    def __init__(self, conflicts: Iterable[str]) -> None:
        self.fields: tuple[str, ...] = tuple(conflicts)
        if not self.fields:
            raise ValueError("a policy conflict names at least one field")
        names = ", ".join(self.fields)
        super().__init__(
            self.default_code,
            f"this session was created with other launch settings ({names}); resume without "
            "them, or start a new session",
        )

    @property
    def field(self) -> str:
        """The first differing field."""
        return self.fields[0]

    def to_data(self) -> dict[str, Any]:
        """The ``error.data`` of the protocol refusal."""
        return {"reason": self.code_name, "field": self.field, "fields": list(self.fields)}


def _text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > _MAX_TEXT:
        raise PolicyRefused("invalid_value", name)
    return value.strip()


def _names(name: str, value: object) -> tuple[str, ...]:
    """A list given as a comma string or a sequence: stripped, blanks and repeats dropped."""
    if isinstance(value, str):
        raw: list[object] = list(value.split(","))
    elif isinstance(value, list | tuple):
        raw = []
        for item in value:  # pyright: ignore[reportUnknownVariableType]
            if not isinstance(item, str):
                raise PolicyRefused("invalid_value", name)
            raw.extend(item.split(","))
    else:
        raise PolicyRefused("invalid_value", name)
    names = [str(part).strip() for part in raw]
    return tuple(dict.fromkeys(n for n in names if n))


def _looks_secret(value: str) -> bool:
    return redact_text(value) != value


def _check_cwd(value: str) -> str:
    if not os.path.isabs(value):
        raise PolicyRefused("invalid_value", "cwd")
    return os.path.normpath(value)


def _check_base_url(value: str) -> str:
    try:
        parts = urlsplit(value)
    except ValueError:
        raise PolicyRefused("invalid_value", "base_url") from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise PolicyRefused("invalid_value", "base_url")
    if parts.username is not None or parts.password is not None or _looks_secret(value):
        raise PolicyRefused("secret_in_policy", "base_url")
    return value


def _check_key_ref(value: str) -> str:
    if _looks_secret(value) or not _REF.fullmatch(value):
        raise PolicyRefused("secret_in_policy", "key_ref")
    return value


@dataclass(frozen=True, slots=True)
class CreationPolicy:
    """A session's launch choices (see the module notes)."""

    cwd: str | None = None
    model: str | None = None
    provider: str | None = None
    reasoning: str | None = None
    toolsets: tuple[str, ...] | None = None
    max_turns: int | None = None
    skills: tuple[str, ...] = ()
    checkpoints: bool = False
    yolo: bool = False
    accept_hooks: bool = False
    ignore_rules: bool = False
    base_url: str | None = None
    key_ref: str | None = None
    """Where the key lives (an env var or secret name), never the key itself."""
    safe_mode: bool = False
    ignore_user_config: bool = False
    pass_session_id: bool = False

    @property
    def bypass(self) -> bool:
        """True when the launch reads no profile defaults (safe mode, ignore-user-config)."""
        return self.safe_mode or self.ignore_user_config

    @classmethod
    def from_launch(cls, launch: Mapping[str, Any]) -> CreationPolicy:
        """Validate and normalise the fields a launch names (unknown names are refused)."""
        return cls(**_parse(launch, strict=True))

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> CreationPolicy:
        """A stored record; unknown or malformed entries are ignored."""
        values: dict[str, Any] = {}
        for name in FROZEN_FIELDS:
            if name not in record:
                continue
            try:
                values.update(_parse({name: record[name]}, strict=False))
            except PolicyRefused:
                continue
        return cls(**values)

    def given(self) -> dict[str, Any]:
        """The fields that differ from "configured default / not asked for"."""
        out: dict[str, Any] = {}
        for name in FROZEN_FIELDS:
            value = getattr(self, name)
            if value is None or value is False or (value == () and name != "toolsets"):
                continue  # an empty toolset selection is a choice; no skills is the default
            out[name] = value
        return out

    def record(self) -> dict[str, Any]:
        """The JSON record kept in ``model_config["launch"]`` (every field, lists as lists)."""
        out: dict[str, Any] = {}
        for item in fields(self):
            value: object = getattr(self, item.name)
            if isinstance(value, tuple):
                value = list(cast(tuple[str, ...], value))
            out[item.name] = value
        return out


def _parse(launch: Mapping[str, Any], *, strict: bool) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for name, value in launch.items():
        if name in _DROPPED:
            continue  # a key is never frozen, compared or stored
        if name not in FROZEN_FIELDS:
            if strict:
                raise PolicyRefused("invalid_field", name if len(name) <= 64 else name[:64])
            continue
        if value is None:
            continue
        if name in _SWITCHES:
            if not isinstance(value, bool):
                raise PolicyRefused("invalid_value", name)
            values[name] = value
        elif name in _LISTS:
            names = _names(name, value)
            if name == "toolsets" or names:  # an empty toolset selection is a choice
                values[name] = names
        elif name == "max_turns":
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise PolicyRefused("invalid_value", name)
            values[name] = value
        else:
            assert name in _TEXTS
            text = _text(name, value)
            if name == "cwd":
                text = _check_cwd(text)
            elif name == "base_url":
                text = _check_base_url(text)
            elif name == "key_ref":
                text = _check_key_ref(text)
            values[name] = text
    return values


def check_launch(policy: CreationPolicy) -> None:
    """A bypass launch must name both the model and the provider (it reads no defaults)."""
    if not policy.bypass:
        return
    for name in ("model", "provider"):
        if not getattr(policy, name):
            raise PolicyRefused("bypass_needs_route", name)


def _same(name: str, frozen: object, requested: object) -> bool:
    if name in _LISTS:
        return sorted(frozen or ()) == sorted(requested or ())  # pyright: ignore[reportUnknownArgumentType, reportArgumentType]
    return frozen == requested


def check_resume(frozen: CreationPolicy, requested: CreationPolicy) -> PolicyConflict | None:
    """Each field ``requested`` names whose value is not the frozen one, as a conflict."""
    differs = [
        name
        for name, value in requested.given().items()
        if not _same(name, getattr(frozen, name), value)
    ]
    return PolicyConflict(differs) if differs else None


def require_same(frozen: CreationPolicy | None, requested: CreationPolicy | None) -> None:
    """Raise ``PolicyConflict`` when a resume changes a frozen value (nothing frozen counts as
    "the configured default", so any explicit value differs)."""
    if requested is None:
        return
    conflict = check_resume(frozen or CreationPolicy(), requested)
    if conflict is not None:
        raise conflict


# -- environment ----------------------------------------------------------------------------------


def launch_flags_from_env(env: Mapping[str, str]) -> dict[str, bool]:
    """``{field: True}`` for each launch env var set to a truthy value."""
    return {
        field: True
        for name, field in LAUNCH_ENV.items()
        if env.get(name, "").strip().lower() in _TRUTHY
    }


def scrub_launch_env(env: Mapping[str, str]) -> dict[str, str]:
    """A copy of ``env`` without the launch-option vars (they belong to one session)."""
    return {name: value for name, value in env.items() if name not in LAUNCH_ENV}


# -- storage --------------------------------------------------------------------------------------


def stored_policy(row: Mapping[str, Any] | None) -> CreationPolicy | None:
    """The frozen policy of a session row (``sessions.get``), or None when none was recorded."""
    if row is None:
        return None
    config = sessions.parse_model_config(row.get("model_config"))
    record = config.get(LAUNCH_KEY)
    if not isinstance(record, Mapping):
        return None
    return CreationPolicy.from_record(record)  # pyright: ignore[reportUnknownArgumentType]


def policy_patch(policy: CreationPolicy) -> dict[str, Any]:
    """The ``model_config`` patch that records ``policy``."""
    return {LAUNCH_KEY: policy.record()}
