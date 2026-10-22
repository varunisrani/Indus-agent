"""Short private folders for Unix sockets in tests (W46, release checklist RS11).

A Unix socket path must fit ``sun_path`` (108 bytes on Linux, 104 on macOS). ``tmp_path`` under a
long ``--basetemp`` (or a long ``TMPDIR``) does not, so a test that binds a socket asks for
``short_dir()`` (or the ``short_tmp`` fixture) instead: a fresh owner-only folder directly under
``/tmp``, removed afterwards. Hosts without ``/tmp`` fall back to the system temp folder.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

SHORT_ROOT = "/tmp"  # noqa: S108 - deliberately short; the folder itself is private (mkdtemp)


@contextlib.contextmanager
def short_dir(prefix: str = "it-") -> Iterator[Path]:
    root = SHORT_ROOT if os.path.isdir(SHORT_ROOT) else None
    path = Path(tempfile.mkdtemp(prefix=prefix, dir=root))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
