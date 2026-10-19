"""Settings and token counting for ``indus research compress`` (P22-11).

``CompressionConfig`` is read from YAML in sections; every key is known, so a misspelt or retired
key is an error instead of a setting that silently does nothing (R10.9). The package ships the
defaults as a real file (``compress_defaults.yaml``), and a file the user gives only needs the
keys it changes.

Tokenizer: a Hugging Face tokenizer named by repository **and revision**, loaded through
``transformers.AutoTokenizer.from_pretrained`` (the optional ``research`` extra). Remote code is
off unless the user opts in (``--trust-remote-code`` or ``tokenizer.trust_remote_code: true``);
opting in prints a warning naming the repository and revision (D-16.21, R10.10). A tokenizer that
cannot load is fatal (``TokenizerError``, exit 2); a turn the tokenizer cannot encode is counted
as ``len(text) // 4`` and the counter's ``fallbacks`` goes up.

Summariser (D-16.60): with no ``summarization.model`` the profile's auxiliary model answers
(``auxiliary.compression``, else the main route).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from indus.foundation.yamlio import YamlError, load_safe

__all__ = [
    "DEFAULTS_FILE",
    "CompressionConfig",
    "ConfigError",
    "TokenCounter",
    "Tokenizer",
    "TokenizerError",
    "load_config",
    "load_tokenizer",
    "transformers_loader",
]

log = logging.getLogger(__name__)

DEFAULTS_FILE = "compress_defaults.yaml"
DEFAULT_TOKENIZER = "Qwen/Qwen3-8B"
DEFAULT_REVISION = "b968826d9c46dd6066d109eabc6255188de91218"
NOTICE_TEXT = (
    "\n\nNote: part of this conversation's earlier tool activity has been condensed into a "
    "summary to keep it within the context budget."
)
CHARS_PER_TOKEN = 4


class ConfigError(ValueError):
    """The compression config cannot be used (CLI exit 2)."""


class TokenizerError(RuntimeError):
    """The tokenizer cannot be loaded (CLI exit 2)."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_default=True)


class TokenizerSettings(_Strict):
    name: str = Field(default=DEFAULT_TOKENIZER, min_length=1)
    revision: str | None = Field(default=DEFAULT_REVISION)
    trust_remote_code: bool = False


class CompressionSettings(_Strict):
    target_max_tokens: int = Field(default=15250, gt=0)
    summary_target_tokens: int = Field(default=750, gt=0)


class ProtectedTurns(_Strict):
    first_system: bool = True
    first_human: bool = True
    first_gpt: bool = True
    first_tool: bool = True
    last_n: int = Field(default=4, ge=0)


class SummarizationSettings(_Strict):
    model: str | None = None
    base_url: str | None = None
    api_key_secret: str | None = None
    temperature: float = Field(default=0.3, ge=0.0, le=2.0)
    max_retries: int = Field(default=3, ge=1)
    retry_delay: float = Field(default=2.0, ge=0.0)


class OutputSettings(_Strict):
    add_summary_notice: bool = True
    notice_text: str = NOTICE_TEXT
    suffix: str = Field(default="_compressed", min_length=1)


class ProcessingSettings(_Strict):
    max_concurrent_requests: int = Field(default=50, ge=1)
    per_trajectory_timeout: float = Field(default=300.0, gt=0.0)
    skip_under_target: bool = True


class MetricsSettings(_Strict):
    enabled: bool = True
    per_trajectory: bool = True
    file: str = Field(default="compression_metrics.json", min_length=1)


class CompressionConfig(_Strict):
    tokenizer: TokenizerSettings = Field(default_factory=TokenizerSettings)
    compression: CompressionSettings = Field(default_factory=CompressionSettings)
    protected_turns: ProtectedTurns = Field(default_factory=ProtectedTurns)
    summarization: SummarizationSettings = Field(default_factory=SummarizationSettings)
    output: OutputSettings = Field(default_factory=OutputSettings)
    processing: ProcessingSettings = Field(default_factory=ProcessingSettings)
    metrics: MetricsSettings = Field(default_factory=MetricsSettings)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any], *, source: str = "config") -> CompressionConfig:
        try:
            return cls.model_validate(dict(data))
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in error['loc']) or '(top)'}: {error['msg']}"
                for error in exc.errors()
            )
            raise ConfigError(f"{source}: {problems}") from None


def defaults_text() -> str:
    """The shipped defaults file."""
    return resources.files("indus.research").joinpath(DEFAULTS_FILE).read_text(encoding="utf-8")


def _parse(text: str, source: str) -> CompressionConfig:
    try:
        data = load_safe(text)
    except YamlError as exc:
        raise ConfigError(f"{source}: not valid YAML ({exc})") from None
    if data is None:
        data = {}
    if not isinstance(data, Mapping):
        raise ConfigError(f"{source}: the file must hold a mapping of sections")
    return CompressionConfig.from_mapping(cast(Mapping[str, Any], data), source=source)


def load_config(path: Path | None = None) -> CompressionConfig:
    """The settings in ``path`` (missing keys keep their defaults), or the shipped defaults."""
    if path is None:
        return _parse(defaults_text(), DEFAULTS_FILE)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read the compression config {path}: {exc}") from None
    return _parse(text, str(path))


# -- tokenizer -------------------------------------------------------------------------------------


class Tokenizer(Protocol):
    def encode(self, text: str) -> Any: ...


type Loader = Callable[..., Tokenizer]


def transformers_loader() -> Loader:
    try:
        from transformers import AutoTokenizer  # pyright: ignore[reportMissingImports]
    except ImportError as exc:
        raise TokenizerError(
            "counting tokens needs the 'transformers' package: install the research extra "
            "(pip install 'indus-agent[research]' or uv sync --extra research)"
        ) from exc
    return cast(Loader, AutoTokenizer.from_pretrained)  # pyright: ignore[reportUnknownMemberType]


@dataclass
class TokenCounter:
    """Token counts with the length // 4 fallback for a turn the tokenizer refuses."""

    tokenizer: Tokenizer
    name: str = ""
    fallbacks: int = 0

    def count(self, text: str) -> int:
        if not text:
            return 0
        try:
            return len(self.tokenizer.encode(text))
        except Exception:
            self.fallbacks += 1
            return len(text) // CHARS_PER_TOKEN


def load_tokenizer(
    settings: TokenizerSettings,
    *,
    loader: Loader | None = None,
    warn: Callable[[str], None] = log.warning,
) -> TokenCounter:
    """The counter for ``settings`` (see the module notes); ``TokenizerError`` when it cannot
    load."""
    revision = settings.revision or "(latest: no revision pinned)"
    if settings.trust_remote_code:
        warn(
            f"warning: the tokenizer {settings.name} at revision {revision} may run code from "
            "its repository on this machine (trust_remote_code is on); only use repositories "
            "you trust"
        )
    load = loader if loader is not None else transformers_loader()
    try:
        tokenizer = load(
            settings.name,
            revision=settings.revision,
            trust_remote_code=settings.trust_remote_code,
        )
    except TokenizerError:
        raise
    except Exception as exc:
        raise TokenizerError(
            f"the tokenizer {settings.name} ({revision}) could not be loaded: {exc}"
        ) from None
    return TokenCounter(tokenizer, name=settings.name)
