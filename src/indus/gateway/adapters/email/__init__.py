"""Email over IMAP and SMTP (P16-09); see ``adapter`` and ``parsing`` for the details."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from indus.gateway.adapters.email.adapter import EmailAdapter
    from indus.gateway.config import AdapterConfig

__all__ = ["create_adapter"]


def create_adapter(config: AdapterConfig) -> EmailAdapter:
    """The registry factory: ``platforms.email`` settings and the mail secrets → adapter."""
    from indus.gateway.adapters.email.adapter import EmailAdapter

    return EmailAdapter.from_config(config)
