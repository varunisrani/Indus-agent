"""Media tags in replies and the outbound path policy (P15-18, R06.22).

The policy itself lives in ``indus.safety.media_policy`` so that other surfaces (``indus mcp
serve``, P19-14) check paths with the same code without importing the gateway (layer rule
D-16.246). This module keeps the gateway's import path; every name is the safety module's own.
"""

from __future__ import annotations

from indus.safety.media_policy import (
    CACHE_DIRS,
    FETCH_MAX_BYTES,
    MediaItem,
    MediaKind,
    MediaPolicy,
    MediaRef,
    RemoteFiles,
    Verdict,
    extract_media,
    fetch_remote,
    screen_remote_path,
    strip_media_tags,
)

__all__ = [
    "CACHE_DIRS",
    "FETCH_MAX_BYTES",
    "MediaItem",
    "MediaKind",
    "MediaPolicy",
    "MediaRef",
    "RemoteFiles",
    "Verdict",
    "extract_media",
    "fetch_remote",
    "screen_remote_path",
    "strip_media_tags",
]
