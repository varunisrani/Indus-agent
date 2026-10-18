"""Turn a file into comparison units for the similarity check.

Three unit streams per file, each unit paired with its 1-based source line:

* ``tokens`` – code tokens (comments and layout dropped),
* ``lines``  – meaningful lines (no blanks, comments, imports or lone brackets),
* ``prose``  – lower-cased words of Markdown/text and of code comments and docstrings.
"""

from __future__ import annotations

import io
import re
import tokenize
from bisect import bisect_right
from pathlib import PurePosixPath

Units = tuple[list[str], list[int]]

PY_EXT = frozenset({".py", ".pyi", ".pyw"})
CLIKE_EXT = frozenset(
    {
        ".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs",
        ".go", ".rs", ".java", ".kt", ".swift", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs",
        ".css", ".scss", ".less", ".vue", ".svelte", ".html", ".htm", ".json", ".jsonc",
    }
)  # fmt: skip
HASH_EXT = frozenset(
    {
        ".sh", ".bash", ".zsh", ".fish", ".ps1", ".psm1", ".yaml", ".yml", ".toml", ".ini",
        ".cfg", ".conf", ".rb", ".nix", ".pl", ".r", ".mk", ".dockerfile", ".service",
    }
)  # fmt: skip
PROSE_EXT = frozenset({".md", ".mdx", ".rst", ".txt", ".tex", ".adoc"})
HASH_NAMES = frozenset({"dockerfile", "makefile", "justfile", "procfile", "containerfile"})

WORD = re.compile(r"[a-z0-9_]+")

_STRING = r"\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])*'|`(?:\\.|[^`\\])*`"
_WORDISH = r"[A-Za-z_$][\w$]*|\d[\w.]*"
CLIKE_SCAN = re.compile(
    rf"(?P<c>//[^\n]*|/\*.*?(?:\*/|\Z)|<!--.*?(?:-->|\Z))|(?P<s>{_STRING})|(?P<w>{_WORDISH})|(?P<p>\S)",
    re.S,
)
HASH_SCAN = re.compile(
    rf"(?P<c>#[^\n]*)|(?P<s>{_STRING})|(?P<w>{_WORDISH})|(?P<p>\S)",
    re.S,
)

_DROP_PY = frozenset(
    {
        tokenize.COMMENT,
        tokenize.NL,
        tokenize.NEWLINE,
        tokenize.INDENT,
        tokenize.DEDENT,
        tokenize.ENCODING,
        tokenize.ENDMARKER,
    }
)
_LINE_START = frozenset(
    {tokenize.NEWLINE, tokenize.NL, tokenize.INDENT, tokenize.DEDENT, tokenize.ENCODING}
)


def kind_of(path: str) -> str | None:
    """Classify a file: 'py', 'clike', 'hash', 'prose' or None (not compared)."""
    p = PurePosixPath(path)
    ext = p.suffix.lower()
    if ext in PY_EXT:
        return "py"
    if ext in CLIKE_EXT:
        return "clike"
    if ext in HASH_EXT or p.name.lower() in HASH_NAMES:
        return "hash"
    if ext in PROSE_EXT:
        return "prose"
    return None


def _line_index(text: str) -> list[int]:
    starts = [0]
    find = text.find
    pos = find("\n")
    while pos != -1:
        starts.append(pos + 1)
        pos = find("\n", pos + 1)
    return starts


def words_at(text: str, first_line: int, out_words: list[str], out_lines: list[int]) -> None:
    """Append the lower-cased words of ``text`` (which starts on ``first_line``)."""
    lowered = text.lower()
    if "\n" not in text:
        found = WORD.findall(lowered)
        out_words.extend(found)
        out_lines.extend([first_line] * len(found))
        return
    starts = _line_index(text)
    for m in WORD.finditer(lowered):
        out_words.append(m.group())
        out_lines.append(first_line + bisect_right(starts, m.start()) - 1)


def _ident_heavy(line: str) -> bool:
    parts = line.split()
    if len(parts) < 3:
        return False
    marked = sum(1 for t in parts if any(c in t for c in "_./") or any(c.isdigit() for c in t))
    return marked / len(parts) > 0.7


def prose_units(text: str, ident_filter: bool = False) -> Units:
    words: list[str] = []
    lines: list[int] = []
    for no, line in enumerate(text.splitlines(), 1):
        if ident_filter and _ident_heavy(line):
            continue
        found = WORD.findall(line.lower())
        words.extend(found)
        lines.extend([no] * len(found))
    return words, lines


def _python_units(text: str) -> tuple[Units, Units]:
    """Tokens and prose (comments + string statements) of Python source via ``tokenize``."""
    toks: list[str] = []
    tlines: list[int] = []
    words: list[str] = []
    wlines: list[int] = []
    pending: tokenize.TokenInfo | None = None  # a STRING that started a logical line
    prev_type = tokenize.ENCODING
    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        ttype = tok.type
        if pending is not None and ttype not in (tokenize.COMMENT, tokenize.NL):
            if ttype in (tokenize.NEWLINE, tokenize.ENDMARKER):
                body = re.sub(r"^[A-Za-z]*", "", pending.string)
                words_at(body, pending.start[0], words, wlines)
            pending = None
        if ttype == tokenize.COMMENT:
            words_at(tok.string[1:], tok.start[0], words, wlines)
            continue
        if ttype in _DROP_PY:
            prev_type = ttype
            continue
        if ttype == tokenize.STRING and prev_type in _LINE_START:
            pending = tok
        toks.append(tok.string)
        tlines.append(tok.start[0])
        prev_type = ttype
    return (toks, tlines), (words, wlines)


def _scan_units(text: str, scanner: re.Pattern[str]) -> tuple[Units, Units]:
    toks: list[str] = []
    tlines: list[int] = []
    words: list[str] = []
    wlines: list[int] = []
    starts = _line_index(text)
    for m in scanner.finditer(text):
        line = bisect_right(starts, m.start())
        if m.lastgroup == "c":
            words_at(m.group(), line, words, wlines)
        else:
            toks.append(m.group())
            tlines.append(line)
    return (toks, tlines), (words, wlines)


_PY_IMPORT = re.compile(r"^(?:import|from)\s+[\w.]+")
_JS_IMPORT = re.compile(
    r"^(?:import\b|export\s+(?:type\s+)?(?:\*|\{)[^;]*\bfrom\b|#include\b|use\s+[\w:]+|package\s+[\w.]+;?$)"
)
_JS_BLOCK_START = re.compile(
    r"^(?:import\s+(?:type\s+)?(?:[\w$]+\s*,\s*)?|export\s+(?:type\s+)?)\{[^}]*$"
)
_REQUIRE = re.compile(r"^(?:const|let|var)\s+[^=]+=\s*require\(")
_LONE = re.compile(r"^[\s()\[\]{}<>;,:.]*$")
_COMMENT_PREFIX = {
    "py": ("#",),
    "hash": ("#",),
    "clike": ("//", "/*", "*", "*/", "<!--", "-->"),
}


def meaningful_lines(text: str, style: str) -> Units:
    out: list[str] = []
    nums: list[int] = []
    prefixes = _COMMENT_PREFIX.get(style, ("#",))
    skip_until: str | None = None
    for no, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if skip_until is not None:
            if skip_until in line:
                skip_until = None
            continue
        if not line or line.startswith(prefixes) or _LONE.match(line):
            continue
        if line in ('"""', "'''"):
            continue
        if style == "py" and _PY_IMPORT.match(line):
            if "(" in line and ")" not in line:
                skip_until = ")"
            elif line.endswith("\\"):
                skip_until = ""  # skip exactly the next line
            continue
        if style == "clike":
            if _JS_BLOCK_START.match(line) and " from " not in line:
                skip_until = "}"
                continue
            if _JS_IMPORT.match(line) or _REQUIRE.match(line):
                continue
        out.append(line)
        nums.append(no)
    return out, nums


def extract(path: str, text: str, ident_filter: bool = False) -> dict[str, Units]:
    """Units for every detector that applies to this file (missing key: detector not used)."""
    kind = kind_of(path)
    if kind is None:
        return {}
    if kind == "prose":
        return {"prose": prose_units(text, ident_filter)}
    if kind == "py":
        try:
            tokens, prose = _python_units(text)
        except (tokenize.TokenError, SyntaxError, IndentationError, ValueError):
            tokens, prose = _scan_units(text, HASH_SCAN)
    elif kind == "clike":
        tokens, prose = _scan_units(text, CLIKE_SCAN)
    else:
        tokens, prose = _scan_units(text, HASH_SCAN)
    return {"tokens": tokens, "lines": meaningful_lines(text, kind), "prose": prose}
