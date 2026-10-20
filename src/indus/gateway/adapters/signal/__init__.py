"""Signal through a user-run signal-cli daemon (P16-05); see ``adapter`` for the details."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from indus.gateway.adapters.signal.adapter import SignalAdapter
    from indus.gateway.config import AdapterConfig

__all__ = ["create_adapter"]


def create_adapter(config: AdapterConfig) -> SignalAdapter:
    """The registry factory: ``platforms.signal`` settings and the Signal secrets → adapter."""
    from indus.gateway.adapters.signal.adapter import SignalAdapter

    return SignalAdapter.from_config(config)
