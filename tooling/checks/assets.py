"""Asset hashes: no image, icon, font, sound or video may be byte-identical to a reference file."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

from tooling.checks.common import Context, Result, missing_ref, ref_commit, rel, walk_files

NAME = "assets"
MEDIA_EXT = frozenset(
    {
        # images and icons
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".icns", ".svg", ".tif",
        ".tiff", ".avif", ".heic", ".psd", ".ai",
        # fonts
        ".ttf", ".otf", ".woff", ".woff2", ".eot",
        # sounds
        ".mp3", ".wav", ".ogg", ".oga", ".flac", ".m4a", ".aac", ".opus", ".aiff",
        # video and animation
        ".mp4", ".webm", ".mov", ".avi", ".mkv", ".m4v", ".lottie",
    }
)  # fmt: skip


def is_media(path: Path) -> bool:
    return path.suffix.lower() in MEDIA_EXT


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def media_hashes(base: Path) -> dict[str, list[str]]:
    """sha256 -> relative paths of media files under base."""
    out: dict[str, list[str]] = {}
    for path in walk_files(base):
        if is_media(path):
            try:
                out.setdefault(sha256(path), []).append(rel(path, base))
            except OSError:
                continue
    return out


def ref_hashes(ref: Path, notes: list[str]) -> dict[str, list[str]]:
    env = os.environ.get("INDUS_CHECKS_CACHE")
    cache = Path(env).expanduser() if env else Path.home() / ".cache" / "indus-checks"
    src = hashlib.blake2b(Path(__file__).read_bytes(), digest_size=6).hexdigest()
    path = cache / f"{ref_commit(ref)}-{src}-assets.json"
    if path.is_file():
        try:
            data: dict[str, list[str]] = json.loads(path.read_text(encoding="utf-8"))
            notes.append(f"reference asset hashes loaded from cache ({len(data)} files)")
            return data
        except (OSError, json.JSONDecodeError):
            pass
    data = media_hashes(ref)
    try:
        cache.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass
    notes.append(f"hashed {sum(len(v) for v in data.values())} reference media files")
    return data


def run(ctx: Context) -> Result:
    result = Result(NAME)
    if missing_ref(ctx, result):
        return result
    assert ctx.ref is not None
    theirs = ref_hashes(ctx.ref, result.notes)
    ours = media_hashes(ctx.root)
    result.notes.append(f"compared {sum(len(v) for v in ours.values())} product media files")
    for digest, paths in sorted(ours.items()):
        if digest in theirs:
            for p in paths:
                result.fail(
                    f"{p} is identical to reference {theirs[digest][0]} (sha256 {digest[:16]}…);"
                    " draw or source our own asset"
                )
    return result


if __name__ == "__main__":
    from tooling.checks.__main__ import main

    raise SystemExit(main([NAME, *sys.argv[1:]]))
