"""The Discord platform (P15-34): ``create_adapter`` is the registry's factory for ``discord``.

discord.py (the ``discord`` extra) is imported only when the adapter connects; the policy
variables come from the profile's environment and the bot token from its secret store or
environment (``AdapterConfig.settings.credentials``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from indus.gateway.adapters.discord.adapter import DiscordAdapter

if TYPE_CHECKING:
    from indus.gateway.config import AdapterConfig

__all__ = ["DiscordAdapter", "create_adapter"]


def create_adapter(config: AdapterConfig, *, sdk: Any = None) -> DiscordAdapter:
    """The Discord adapter for one profile. ``sdk`` stands in for the ``discord`` module."""
    scope = config.scope

    async def is_paired(user_id: str) -> bool:
        from indus.foundation.blocking import run_blocking
        from indus.state.pairing import PairingStore

        store = PairingStore(scope)
        return await run_blocking(
            store.is_approved, "discord", user_id, label="discord pairing check"
        )

    return DiscordAdapter(
        clock=config.clock,
        settings=config.settings,
        env=scope.env,
        sdk=sdk,
        send_only=config.send_only,
        proxy=config.proxy,
        is_paired=is_paired,
    )
