"""The Slack adapter (P15-35): Socket Mode in, Web API out.

The registry builds it through ``create_adapter`` (``indus.gateway.adapters.slack:create_adapter``)
only for an enabled platform; importing this package never imports the Slack libraries, which load
on the first connect (the ``slack`` extra).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from indus.gateway.adapters.slack.adapter import SlackAdapter

if TYPE_CHECKING:
    from indus.gateway.config import AdapterConfig

__all__ = ["SlackAdapter", "create_adapter"]


def create_adapter(config: AdapterConfig) -> SlackAdapter:
    """The adapter for one profile's Slack settings (send-only when ``config.send_only``)."""
    return SlackAdapter.from_config(config)
