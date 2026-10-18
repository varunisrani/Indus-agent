"""Config: schema and defaults, the one loader, the one writer, backups, migrations, dotted keys.

Read with `load(scope)` (a read-only `ConfigView`); write with `save(scope, data)`. Nothing else
reads or writes ``config.yaml``.
"""

from __future__ import annotations

from indus.foundation.config.loader import (
    CONFIG_FILE,
    ConfigScope,
    ConfigView,
    FailedRead,
    config_path,
    load,
)
from indus.foundation.config.schema import CURRENT_VERSION, DEFAULTS, defaults_tree
from indus.foundation.config.writer import save

__all__ = [
    "CONFIG_FILE",
    "CURRENT_VERSION",
    "DEFAULTS",
    "ConfigScope",
    "ConfigView",
    "FailedRead",
    "config_path",
    "defaults_tree",
    "load",
    "save",
]
