"""The release notes and the contributor map (P23-22, R10.29).

Notes list the commits since the previous release tag (merges left out), grouped by the kind a
commit subject starts with (``feat`` → Features, ``fix`` → Fixes, ``docs`` → Documentation,
``perf``/``refactor`` → Improvements, anything else → Other changes), and credit their authors.

Credits come from ``contributors/``: one file per author email, named by the first 16 hex digits
of SHA-256 of the lower-cased address (``<key>.txt``), holding the name to credit on its first
line. No address appears in a file name or in the notes. The map starts empty; a stable release
whose commits have an unmapped author is refused (``unmapped``), and the refusal prints each key
and file to create.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "CONTRIBUTORS_DIR",
    "Commit",
    "categorize",
    "commits_between",
    "contributor_key",
    "load_contributors",
    "render",
    "unmapped",
]

CONTRIBUTORS_DIR = "contributors"
_SEP = "\x1f"
_END = "\x1e"
_KINDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Features", ("feat", "feature")),
    ("Fixes", ("fix", "bugfix", "hotfix")),
    ("Improvements", ("perf", "refactor", "improve")),
    ("Documentation", ("docs", "doc")),
)
_OTHER = "Other changes"
_PREFIX = re.compile(r"^(?P<kind>[a-z]+)(?:\([^)]*\))?!?:\s*", re.IGNORECASE)


@dataclass(frozen=True)
class Commit:
    sha: str
    author: str
    email: str
    subject: str


def commits_between(git: Callable[..., str], since: str | None, until: str) -> list[Commit]:
    """Non-merge commits in ``since..until`` (all of ``until`` when ``since`` is None), oldest
    first. ``git(*args)`` returns stdout."""
    span = f"{since}..{until}" if since else until
    raw = git(
        "log", "--no-merges", "--reverse", f"--format=%H{_SEP}%an{_SEP}%ae{_SEP}%s{_END}", span
    )
    found: list[Commit] = []
    for record in raw.split(_END):
        parts = record.strip("\n").split(_SEP)
        if len(parts) == 4 and parts[0]:
            found.append(Commit(*parts))
    return found


def categorize(subject: str) -> str:
    match = _PREFIX.match(subject.strip())
    word = match.group("kind").lower() if match else ""
    for title, words in _KINDS:
        if word in words:
            return title
    return _OTHER


def _clean(subject: str) -> str:
    text = _PREFIX.sub("", subject.strip(), count=1) if _PREFIX.match(subject.strip()) else subject
    text = text.strip()
    return text[:1].upper() + text[1:]


def contributor_key(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()[:16]


def load_contributors(directory: Path) -> dict[str, str]:
    """``key → credit name`` from ``<directory>/<key>.txt`` files (other files are ignored)."""
    found: dict[str, str] = {}
    if not directory.is_dir():
        return found
    for path in sorted(directory.glob("*.txt")):
        if not re.fullmatch(r"[0-9a-f]{16}", path.stem):
            continue
        lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
        name = next((line for line in lines if line), "")
        if name:
            found[path.stem] = name
    return found


def unmapped(commits: Iterable[Commit], contributors: Mapping[str, str]) -> list[str]:
    """The keys of commit authors with no entry, each once, in commit order."""
    missing: list[str] = []
    for commit in commits:
        key = contributor_key(commit.email)
        if key not in contributors and key not in missing:
            missing.append(key)
    return missing


def render(
    version: str,
    commits: Sequence[Commit],
    contributors: Mapping[str, str],
    *,
    previous_tag: str | None,
    repository: str,
) -> str:
    """Markdown notes: one section per kind, then the credited authors."""
    lines = [f"# Indus Agent {version}", ""]
    if previous_tag:
        lines += [f"Changes since {previous_tag}.", ""]
    groups: dict[str, list[Commit]] = {}
    for commit in commits:
        groups.setdefault(categorize(commit.subject), []).append(commit)
    for title in [*(t for t, _w in _KINDS), _OTHER]:
        rows = groups.get(title, [])
        if not rows:
            continue
        lines += [f"## {title}", ""]
        for commit in rows:
            who = contributors.get(contributor_key(commit.email))
            credit = f" ({who})" if who else ""
            url = f"https://github.com/{repository}/commit/{commit.sha}"
            lines.append(f"- {_clean(commit.subject)} ([`{commit.sha[:8]}`]({url})){credit}")
        lines.append("")
    names = sorted(
        {contributors[k] for c in commits if (k := contributor_key(c.email)) in contributors}
    )
    if names:
        lines += ["## Thanks", "", *(f"- {name}" for name in names), ""]
    if not commits:
        lines += ["No changes since the previous release.", ""]
    return "\n".join(lines)
