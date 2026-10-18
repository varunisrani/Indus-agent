"""Config errors. The classes and their codes live in ``foundation.errors``; re-exported here."""

from __future__ import annotations

from indus.foundation.errors import ConfigError, ConfigFailedRead, ConfigManaged, ConfigUnreadable

__all__ = ["ConfigError", "ConfigFailedRead", "ConfigManaged", "ConfigUnreadable"]
