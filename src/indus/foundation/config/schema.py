"""Config schema: Pydantic models, the one defaults table and the commented example.

`DEFAULTS` is generated from the models, so every default lives in exactly one place (D-16.32).
Sections without a model yet are kept as raw mappings; unknown keys inside a modelled section are
kept and reported, never dropped.
"""

from __future__ import annotations

import copy
import types
import typing
from collections.abc import Mapping
from typing import Any, Literal, Union, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from indus.foundation import yamlio

CURRENT_VERSION = 1
VERSION_KEY = "_config_version"


class _Section(BaseModel):
    model_config = ConfigDict(extra="allow", validate_default=True)


class ModelSection(_Section):
    """Which model to talk to; ``model: <id>`` in the file is shorthand for ``default``."""

    default: str = Field(default="", description="model id used when nothing else picks one")
    provider: str = Field(default="", description="provider name; empty means detect from keys")
    base_url: str = Field(default="", description="custom endpoint URL (OpenAI-compatible)")
    api_mode: str = Field(default="", description="wire format override for the endpoint")
    context_length: int | None = Field(
        default=None, description="context window override in tokens"
    )


class AgentSection(_Section):
    max_turns: int = Field(
        default=150, description="model calls per turn before a summary; 0 = no cap"
    )
    api_max_retries: int = Field(default=3, description="retries for a failed model call")
    gateway_timeout: int = Field(default=1800, description="seconds a messaging turn may run")
    clarify_timeout: int = Field(
        default=3600, description="seconds to wait for an answer to a question"
    )
    local_stream_stale_timeout: int = Field(
        default=900, description="seconds a local model server may stay silent mid-stream"
    )


class TerminalSection(_Section):
    backend: str = Field(default="local", description="where commands run: local, docker, ssh, ...")
    cwd: str = Field(default=".", description="working directory for commands")
    timeout: int = Field(default=180, description="seconds before a command is stopped")


class ApprovalsSection(_Section):
    mode: Literal["manual", "smart", "off"] = Field(
        default="manual",
        description="manual asks before risky commands; smart pre-judges; off never asks",
    )
    timeout: int = Field(default=300, description="seconds to wait for an approval answer")
    deny: list[str] = Field(
        default_factory=list, description="command patterns that are always refused"
    )


class DatabaseSection(_Section):
    journal_mode: Literal["wal", "delete"] = Field(default="wal", description="SQLite journal mode")
    pragmas: dict[str, Any] = Field(default_factory=dict, description="extra SQLite pragmas")


class SessionsSection(_Section):
    retention_days: int = Field(default=90, description="days ended sessions are kept")
    auto_prune: bool = Field(default=False, description="delete old sessions automatically")
    resume_message_limit: int = Field(default=20000, description="messages loaded when resuming")


class LoggingSection(_Section):
    level: str = Field(default="INFO", description="level for agent.log")
    max_size_mb: int = Field(default=5, description="size of one log file before it rotates")
    backup_count: int = Field(default=3, description="rotated files kept")


class SecuritySection(_Section):
    redact_secrets: bool = Field(default=True, description="mask secrets in logs and tool output")
    secret_store: Literal["auto", "keychain", "file"] = Field(
        default="auto",
        description="where API keys live: OS keychain first (auto), keychain or file",
    )


class DisplaySection(_Section):
    language: str = Field(default="en", description="interface language")


class NetworkSection(_Section):
    force_ipv4: bool = Field(default=False, description="connect over IPv4 only")


class ModelCatalogSection(_Section):
    ttl_minutes: int = Field(
        default=20, description="minutes a provider's live model list is reused before refetching"
    )


class ConfigModel(BaseModel):
    """The whole file. Unknown top-level sections are kept as they are."""

    model_config = ConfigDict(extra="allow", validate_default=True, populate_by_name=True)

    model: str | ModelSection = Field(
        default_factory=ModelSection, description="the chat model and its endpoint"
    )
    providers: dict[str, dict[str, Any]] = Field(
        default_factory=dict, description="named custom providers"
    )
    agent: AgentSection = Field(default_factory=AgentSection, description="the turn loop")
    terminal: TerminalSection = Field(
        default_factory=TerminalSection, description="where and how commands run"
    )
    approvals: ApprovalsSection = Field(
        default_factory=ApprovalsSection, description="asking before risky commands"
    )
    database: DatabaseSection = Field(
        default_factory=DatabaseSection, description="the session store"
    )
    sessions: SessionsSection = Field(
        default_factory=SessionsSection, description="keeping and resuming sessions"
    )
    logging: LoggingSection = Field(
        default_factory=LoggingSection, description="log files under <home>/logs"
    )
    security: SecuritySection = Field(
        default_factory=SecuritySection, description="secrets and redaction"
    )
    display: DisplaySection = Field(default_factory=DisplaySection, description="the interface")
    network: NetworkSection = Field(default_factory=NetworkSection, description="connections")
    model_catalog: ModelCatalogSection = Field(
        default_factory=ModelCatalogSection, description="provider model lists"
    )
    timezone: str = Field(
        default="", description="IANA zone name, e.g. Asia/Kolkata; empty = system zone"
    )
    config_version: int = Field(default=CURRENT_VERSION, alias=VERSION_KEY)


def _build_defaults() -> dict[str, Any]:
    return ConfigModel().model_dump(by_alias=True, mode="python")


_DEFAULTS: dict[str, Any] = _build_defaults()
DEFAULTS: Mapping[str, Any] = types.MappingProxyType(_DEFAULTS)


def defaults_tree() -> dict[str, Any]:
    """A fresh deep copy of the defaults, safe to mutate."""
    return copy.deepcopy(_DEFAULTS)


_MISSING = object()


def default_for(key: str, fallback: Any = None) -> Any:
    """The default at a dotted path (plain segments only), or ``fallback``."""
    node: Any = _DEFAULTS
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return fallback
        node = typing.cast(dict[str, Any], node)[part]
    return copy.deepcopy(node)


def _strip_optional(annotation: Any) -> Any:
    origin = typing.get_origin(annotation)
    if origin in (Union, types.UnionType):
        members = [a for a in typing.get_args(annotation) if a is not type(None)]
        return members[0] if members else annotation
    return annotation


def _type_of(raw_annotation: Any) -> type | None:
    annotation: object = _strip_optional(raw_annotation)
    origin = typing.get_origin(annotation)
    if origin is Literal:
        values: tuple[object, ...] = typing.get_args(annotation)
        return type(values[0]) if values else str
    if origin in (list, tuple, set):
        return list
    if origin in (dict, Mapping):
        return dict
    if isinstance(annotation, type):
        plain = cast(type[object], annotation)
        return dict if issubclass(plain, BaseModel) else plain
    return None


def schema_for(key: str) -> type | None:
    """The type a value at a dotted path should have (``str``, ``int``, ``list``, ``dict``...).

    Used by `config set` coercion. Unknown paths give ``None``.
    """
    model: type[BaseModel] | None = ConfigModel
    annotation: Any = None
    for part in key.split("."):
        if model is None:
            # Inside a free-form mapping (``providers.x``) the shape is open.
            return None
        field = None
        for name, info in model.model_fields.items():
            if part in (name, info.alias):
                field = info
                break
        if field is None:
            return None
        annotation = field.annotation
        is_union = typing.get_origin(annotation) in (Union, types.UnionType)
        members = [
            m
            for m in (typing.get_args(annotation) if is_union else (annotation,))
            if m is not type(None)
        ]
        # ``model`` is ``str | ModelSection``: the shorthand string is the slot's own type, and
        # its sub-keys come from the section model.
        model = next((m for m in members if isinstance(m, type) and issubclass(m, BaseModel)), None)
        annotation = members[0] if members else annotation
    return _type_of(annotation)


def validate(data: Mapping[str, Any]) -> list[str]:
    """Check ``data`` against the models; return warnings (never raises, never drops keys)."""
    warnings: list[str] = []
    try:
        parsed = ConfigModel.model_validate(dict(data))
    except ValidationError as exc:
        for err in exc.errors():
            where = ".".join(str(p) for p in err["loc"])
            warnings.append(f"{where}: {err['msg']}")
        return warnings
    for name in ConfigModel.model_fields:
        section = getattr(parsed, name)
        if isinstance(section, _Section) and section.model_extra:
            for extra in section.model_extra:
                warnings.append(f"{name}.{extra}: unknown setting (kept)")
    return warnings


_EXAMPLE_HEADER = """\
# Indus Agent settings.
#
# Every setting is shown below with its built-in value, commented out. Uncomment a line and change
# it to override the built-in value; anything left commented keeps following the built-in value,
# also when a later release changes it. Secrets (API keys) belong in the keychain or .env, never
# here; refer to one with ${NAME} if a setting needs it.
"""


def render_example() -> str:
    """The commented example text that starts every newly created settings file."""
    parts = [_EXAMPLE_HEADER]
    for name, info in ConfigModel.model_fields.items():
        key = info.alias or name
        if key == VERSION_KEY:
            continue
        body = yamlio.dump_plain({key: _DEFAULTS[key]})
        lines = [f"# {line}" if line else "#" for line in body.rstrip("\n").split("\n")]
        parts.append("")
        if info.description:
            parts.append(f"# -- {info.description}")
        parts.extend(lines)
    return "\n".join(parts) + "\n"
