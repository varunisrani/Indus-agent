"""File checkpoints: the shared shadow git store, the once-per-turn trigger, rollback and
pruning (P11-15..18). ``install()`` makes the real store the one ``indus checkpoints`` uses."""

from __future__ import annotations

from indus.backends import checkpoint_api

__all__ = ["install"]


def install() -> None:
    """Register the shadow store with ``checkpoint_api.set_store_factory``."""
    from indus.backends.checkpoints.store import store_factory

    checkpoint_api.set_store_factory(store_factory)
