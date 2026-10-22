"""Fixtures for the security regression suite (P25-02).

The suite drives each trust boundary of ``docs/internals/threat-model.md`` at its product entry
point with fakes only: no network (resolvers are injected), no real home (the root conftest's
``isolated_home``), fake credentials built at run time (never a credential-shaped literal).
The dashboard builder is the one the web unit tests use.
"""

from __future__ import annotations

from tests.unit.web.conftest import build, bundle_dir

__all__ = ["build", "bundle_dir", "fake_key", "fake_token"]


def fake_key() -> str:
    """A provider-key-shaped value, assembled here so no source file holds one."""
    return "s" + "k-" + "proj" + "-" + "Q7" * 20


def fake_token() -> str:
    """A bot-token-shaped value (digits, a colon, 35 characters), assembled at run time."""
    return "1234567" + "890" + ":" + "AAH" + "x" * 32
