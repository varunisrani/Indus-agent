"""Scan text that will reach the model for prompt-injection and exfiltration shapes.

``scan(text, scope=...)`` returns finding ids, empty when nothing matched. It is advisory and
cheap: only the first ``SCAN_LIMIT`` characters are looked at. Two pattern sets:

- ``context`` — for text the user did not write and the model will treat as instructions
  (project context files, the persona file). Patterns stay anchored on attack behaviour, never on
  merely bossy English: "you must run the tests" is normal in an AGENTS.md.
- ``strict`` — ``context`` plus persistence shapes that only make sense to block where a person
  approves the write (memory, skill installs).

``memory`` and ``skill`` name the strict set for the learning loop's own writes (P09): a saved
memory entry or an agent-written skill reaches every later session, so it gets the strict set.
``cron`` names it for scheduled jobs (P14): their prompts run unattended, again and again.

Invisible and bidirectional control characters are reported one id per code point
(``invisible:U+200B``) and are checked on the raw text, because NFKC folding (used for the
patterns, so full-width letters cannot dodge them) removes some of them.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

__all__ = [
    "HIDDEN_CHARACTERS",
    "SCAN_LIMIT",
    "TAG_FIRST",
    "TAG_LAST",
    "Hit",
    "Scope",
    "is_hidden_character",
    "locate",
    "scan",
]

Scope = Literal["context", "strict", "memory", "skill", "cron"]

SCAN_LIMIT = 100_000

# Zero-width and joiner characters, invisible math operators, the byte-order mark used mid-text,
# and every bidirectional embedding, override and isolate control. The plain left-to-right and
# right-to-left marks are left out: right-to-left documents use them legitimately.
HIDDEN_CHARACTERS = frozenset(
    chr(code)
    for code in (
        *range(0x200B, 0x200E),  # zero-width space, non-joiner, joiner
        *range(0x2060, 0x2065),  # word joiner, invisible operators
        0xFEFF,  # byte-order mark (a leading one is stripped by callers)
        *range(0x202A, 0x202F),  # bidirectional embeddings and overrides
        *range(0x2066, 0x206A),  # bidirectional isolates
    )
)
# Unicode "tag" characters mirror ASCII invisibly and are used to smuggle instructions.
TAG_FIRST, TAG_LAST = 0xE0000, 0xE007F

# Up to a few words between the key words of a phrase; bounded so the regex cannot backtrack badly.
_GAP = r"(?:[\w'-]+[\s,]+){0,5}?"
# Env var names that hold secrets; matched case-sensitively so "$monkey" is not a key.
_SECRET_NAME = r"(?-i:\$\{?[A-Za-z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS?)\b\}?)"
_NET_TOOL = r"\b(?:curl|wget|ncat|netcat|nc|invoke-webrequest|invoke-restmethod|iwr|irm)\b"
# Where a shell command can start: line start, after a prompt sign, a backtick, ; | & or (.
_CMD_START = r"(?:^|[`;|&(]|\$\s)\s*"
_SECRET_FILE = (
    r"(?:\.env(?![\w.-])|\.netrc\b|\.pgpass\b|\bid_rsa\b|\bid_ed25519\b|\.aws/credentials|"
    r"\.ssh/(?!config\b)|\.git-credentials|\.npmrc\b|\.pypirc\b)"
)
_PRIOR = r"(?:previous|prior|earlier|above|preceding|original|system|developer)"

_CONTEXT_PATTERNS: tuple[tuple[str, str], ...] = (
    (
        "override_instructions",
        rf"\b(?:ignore|disregard|forget|override|bypass)\s+{_GAP}{_PRIOR}\s+"
        rf"{_GAP}(?:instructions?|rules|directions|guidelines|prompts?|guardrails)\b",
    ),
    (
        "override_instructions",
        r"\b(?:ignore|disregard|forget)\s+(?:all|any|every)\s+(?:of\s+)?(?:your\s+|the\s+|my\s+)?"
        r"(?:instructions|prompts|directives)\b",
    ),
    (
        "override_instructions",
        r"\b(?:new|updated|replacement)\s+system\s+(?:prompt|instructions)\s*[:\-]",
    ),
    ("override_instructions", r"\bsystem\s+prompt\s+(?:override|replacement)\b"),
    (
        "conceal_from_user",
        r"\b(?:do\s+not|don't|never)\s+(?:\w+\s+){0,2}?(?:tell|inform|let\s+on\s+to|alert)\s+"
        r"(?:the\s+)?(?:user|human|operator)\b",
    ),
    (
        "conceal_from_user",
        r"\bwithout\s+(?:telling|informing|alerting)\s+(?:the\s+)?(?:user|human|operator)\b",
    ),
    (
        "conceal_from_user",
        rf"\b(?:hide|conceal)\s+{_GAP}(?:from|secret\s+from)\s+the\s+(?:user|human|operator)\b",
    ),
    (
        "role_takeover",
        rf"\byou\s+are\s+(?:now|no\s+longer)\s+{_GAP}(?:a|an|the|in)\s+"
        r"(?:\w+\s+){0,3}(?:mode|assistant|ai|agent|model|persona)\b",
    ),
    ("role_takeover", r"\b(?:developer|jailbreak|god|unrestricted)\s+mode\s+(?:is\s+)?enabled\b"),
    (
        "prompt_leak",
        rf"\b(?:print|reveal|output|repeat|dump|show)\s+{_GAP}(?:your|the)\s+"
        r"(?:full\s+|entire\s+|hidden\s+)?(?:system|initial|original)\s+(?:prompt|instructions)\b",
    ),
    ("secret_exfiltration", rf"{_NET_TOOL}[^\n]{{0,400}}{_SECRET_NAME}"),
    ("secret_exfiltration", rf"{_SECRET_NAME}[^\n]{{0,200}}\|\s*{_NET_TOOL}"),
    (
        "secret_exfiltration",
        rf"\b(?:send|post|upload|forward|exfiltrate|transmit)\s+{_GAP}"
        r"(?:api[\s_-]?keys?|credentials|secrets|tokens|passwords|private\s+keys?)\s+"
        r"(?:\w+\s+){0,4}?(?:to|at)\s+(?:https?://|\S+@\S+|\w+\.\w+)",
    ),
    (
        "secret_file_read",
        rf"{_CMD_START}(?:cat|type|less|more|head|tail|base64|xxd|strings|get-content|gc)\s+"
        rf"[^\n|;>]{{0,80}}?{_SECRET_FILE}",
    ),
    (
        "hidden_html_comment",
        r"<!--[^>]{0,600}?(?:\b(?:ignore|disregard|override)\s+(?:\w+\s+){0,3}?"
        r"(?:instructions|rules|above|previous|everything)\b|\bsystem\s+prompt\b|"
        r"\bdo\s+not\s+tell\b|\bassistant\s*:)",
    ),
    (
        "hidden_html_block",
        r"<\s*(?:div|span|p|section)\b[^>]{0,400}?(?:display\s*:\s*none|visibility\s*:\s*hidden|"
        r"font-size\s*:\s*0(?![.\d]*[1-9]))",
    ),
)

_STRICT_PATTERNS: tuple[tuple[str, str], ...] = (
    ("ssh_key_plant", r"(?:>>|tee\s+-a)\s*[^\n]{0,80}authorized_keys\b"),
    (
        "shell_profile_persist",
        r"(?:>>|tee\s+-a)\s*[^\n]{0,40}(?:\.bashrc|\.zshrc|\.profile|\.bash_profile)\b",
    ),
    ("shell_profile_persist", r"\bcrontab\b[^\n]{0,80}\b(?:curl|wget)\b"),
)


def _compile(pairs: tuple[tuple[str, str], ...]) -> tuple[tuple[str, re.Pattern[str]], ...]:
    flags = re.IGNORECASE | re.MULTILINE
    return tuple((name, re.compile(source, flags)) for name, source in pairs)


_STRICT = _compile(_CONTEXT_PATTERNS + _STRICT_PATTERNS)
_COMPILED: dict[str, tuple[tuple[str, re.Pattern[str]], ...]] = {
    "context": _compile(_CONTEXT_PATTERNS),
    "strict": _STRICT,
    "memory": _STRICT,
    "skill": _STRICT,
    "cron": _STRICT,
}


def is_hidden_character(char: str) -> bool:
    code = ord(char)
    return char in HIDDEN_CHARACTERS or TAG_FIRST <= code <= TAG_LAST


@dataclass(frozen=True)
class Hit:
    """One pattern match: the finding id, its 1-based line and the matched text (NFKC-folded)."""

    id: str
    line: int
    text: str


def _patterns(scope: str) -> tuple[tuple[str, re.Pattern[str]], ...]:
    patterns = _COMPILED.get(scope)
    if patterns is None:
        raise ValueError(f"unknown threat-scan scope: {scope!r}")
    return patterns


def locate(text: str, *, scope: Scope = "context") -> tuple[Hit, ...]:
    """Every pattern match in the first ``SCAN_LIMIT`` characters of ``text``, with its line.

    The same patterns as ``scan`` (hidden characters are left to the caller); used by the content
    scanner (P13-20), which reports findings per file and line. Lines count ``\\n`` only.
    """
    patterns = _patterns(scope)
    if not text:
        return ()
    folded = unicodedata.normalize("NFKC", text[:SCAN_LIMIT])
    hits: list[Hit] = []
    for name, pattern in patterns:
        for match in pattern.finditer(folded):
            line = folded.count("\n", 0, match.start()) + 1
            hits.append(Hit(name, line, match.group(0)))
    hits.sort(key=lambda h: h.line)
    return tuple(hits)


def scan(text: str, *, scope: Scope = "context") -> tuple[str, ...]:
    """Finding ids for ``text`` in ``scope``, in a stable order, each id once."""
    patterns = _patterns(scope)
    if not text:
        return ()
    window = text[:SCAN_LIMIT]
    hidden = sorted({ord(ch) for ch in window if is_hidden_character(ch)})
    found: dict[str, None] = {f"invisible:U+{code:04X}": None for code in hidden}
    folded = unicodedata.normalize("NFKC", window)
    for name, pattern in patterns:
        if name not in found and pattern.search(folded):
            found[name] = None
    return tuple(found)
