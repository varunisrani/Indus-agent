"""Reading untrusted mail: addresses, headers, bodies, attachments, sender authentication (P16-09).

Everything here treats its input as hostile. Decoding never raises (unknown charsets fall back
to UTF-8 and then Latin-1 with replacement characters); a From header longer than
``MAX_FROM_CHARS`` or naming more than one mailbox gives no sender; HTML is turned into text by
the standard parser with scripts and styles dropped and nothing remote ever loaded.

``verify_sender`` decides whether the From domain is authenticated, from the topmost
``Authentication-Results`` header only (the one the receiving server prepends, RFC 8601), and only
when that header names exactly the operator's pinned authserv-id: DMARC pass (with a matching
``header.from``), or a single SPF pass whose ``smtp.mailfrom`` aligns, or a DKIM pass whose
``header.d`` aligns. Comments are dropped and quoted strings kept whole while the header is split,
so text inside them cannot pose as a verdict or a property.
"""

from __future__ import annotations

import codecs
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from email.header import decode_header
from email.message import Message
from email.utils import parseaddr
from html.parser import HTMLParser

__all__ = [
    "MAX_FROM_CHARS",
    "NO_HEADER",
    "NO_PIN",
    "UNTRUSTED",
    "MailPart",
    "attachments_of",
    "body_text",
    "clean_ids",
    "decode_header_value",
    "display_name_of",
    "html_to_text",
    "is_automated",
    "one_line",
    "safe_decode",
    "sender_address",
    "verify_sender",
]

MAX_FROM_CHARS = 2048
_MAX_PARENS = 64
_MAX_IDS = 20

_CHARSET_ALIASES: Mapping[str, str] = {
    "unknown-8bit": "utf-8",
    "unknown": "utf-8",
    "x-unknown": "utf-8",
    "default": "utf-8",
    "gb2312": "gb18030",
    "gbk": "gb18030",
    "ks_c_5601-1987": "cp949",
    "cp-850": "cp850",
}


def _codec_for(label: str | None) -> str:
    """A text codec Python knows for a mail charset label; UTF-8 when it knows none."""
    cleaned = (label or "").strip(" \t\"'").lower()
    cleaned = _CHARSET_ALIASES.get(cleaned, cleaned)
    if not cleaned:
        return "utf-8"
    try:
        codecs.lookup(cleaned)
    except LookupError:
        return "utf-8"
    return cleaned


def safe_decode(payload: bytes, charset: str | None) -> str:
    """``payload`` as text; an unknown or broken charset label falls back, never raises."""
    try:
        return str(payload, _codec_for(charset), "replace")
    except (LookupError, ValueError):  # a bytes-only codec, or one that ignores "replace"
        return str(payload, "utf-8", "replace")


def decode_header_value(raw: object) -> str:
    """An RFC 2047 header value as plain text; malformed encoded words degrade, never raise."""
    text = str(raw or "")
    try:
        parts = decode_header(text)
    except Exception:
        return text
    return "".join(
        safe_decode(value, charset) if isinstance(value, bytes) else str(value)
        for value, charset in parts
    )


_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


def one_line(text: str, limit: int = 998) -> str:
    """``text`` without control characters (no header injection), spaces collapsed, cut."""
    return " ".join(_CONTROL.sub(" ", text).split())[:limit]


# -- addresses -------------------------------------------------------------------------------------

_FOLD = re.compile(r"\r?\n[ \t]+")
_BRACKETED = re.compile(r"([^\"<>]*)<([^<>\s]+)>\s*")
_COMMENT = re.compile(r"\([^()]*\)")


def _without_comments(text: str) -> str:
    while True:
        stripped = _COMMENT.sub(" ", text)
        if stripped == text:
            return text
        text = stripped


def sender_address(raw: object) -> str:
    """The lower-cased address of a From value, or "" when there is no single usable one."""
    value = _FOLD.sub(" ", str(raw or ""))
    if len(value) > MAX_FROM_CHARS or value.count("(") > _MAX_PARENS:
        return ""
    _, address = parseaddr(value)
    if not address:
        match = _BRACKETED.fullmatch(value)
        if match is not None:
            display, inner = _without_comments(match.group(1)).strip(), match.group(2)
            if not any(c in display for c in ';:()"') and (
                "@" not in display or display.lower() == inner.lower()
            ):
                address = inner
    address = address.strip().lower()
    local, at, domain = address.rpartition("@")
    if not at or not local or not domain or "." not in domain or any(c in address for c in " ,;<>"):
        return ""
    return address


def display_name_of(raw: object) -> str:
    value = _FOLD.sub(" ", str(raw or ""))
    if len(value) > MAX_FROM_CHARS:
        return ""
    name, _ = parseaddr(value)
    return one_line(decode_header_value(name), 200)


# -- automated mail --------------------------------------------------------------------------------

_ROBOT_LOCAL_PARTS = (
    "noreply",
    "no-reply",
    "no_reply",
    "donotreply",
    "do-not-reply",
    "do_not_reply",
    "mailer-daemon",
    "postmaster",
    "bounce",
    "auto-reply",
    "autoreply",
    "auto-confirm",
)


def is_automated(address: str, headers: Mapping[str, str] | Message) -> bool:
    """Auto-replies, bounces and list mail: answering them would start a mail loop."""
    local = address.partition("@")[0].lower()
    if any(word in local for word in _ROBOT_LOCAL_PARTS):
        return True

    def get(name: str) -> str:
        value = headers.get(name)
        return str(value).strip().lower() if value is not None else ""

    auto = get("Auto-Submitted")
    if auto and auto != "no":
        return True
    if get("Precedence") in ("bulk", "list", "junk", "auto_reply"):
        return True
    return bool(get("List-Id") or get("List-Unsubscribe"))


# -- bodies ----------------------------------------------------------------------------------------


class _TextOnly(HTMLParser):
    _HIDDEN = frozenset({"script", "style", "head", "title", "template", "noscript", "svg"})
    _BLOCKS = frozenset(
        [
            "p",
            "div",
            "br",
            "tr",
            "table",
            "blockquote",
            "pre",
            "hr",
            "section",
            "article",
            "ul",
            "ol",
            "header",
            "footer",
        ]
    ) | {f"h{level}" for level in range(1, 7)}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._HIDDEN:
            self._hidden += 1
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("br", "hr"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._HIDDEN:
            self._hidden = max(0, self._hidden - 1)
        elif tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._hidden:
            self.parts.append(data)


_BLANKS = re.compile(r"\n{3,}")
_RUNS = re.compile(r"[ \t\u00a0]+")


def html_to_text(html: str) -> str:
    """Readable text of an HTML body: tags, scripts and styles dropped, entities decoded."""
    parser = _TextOnly()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # the stdlib parser is lenient; a failure keeps what it read
        pass
    lines = [_RUNS.sub(" ", line).strip() for line in "".join(parser.parts).splitlines()]
    return _BLANKS.sub("\n\n", "\n".join(lines)).strip()


def _is_attachment(part: Message) -> bool:
    disposition = str(part.get("Content-Disposition") or "").split(";", 1)[0].strip().lower()
    if disposition == "attachment":
        return True
    if part.is_multipart():
        return False
    ctype = part.get_content_type()
    return disposition == "inline" and ctype not in ("text/plain", "text/html")


def _text_of(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if not isinstance(payload, bytes):
        return ""
    return safe_decode(payload, part.get_content_charset())


def body_text(message: Message) -> str:
    """The first plain-text body part, else the first HTML part as text, else ""."""
    plain: str | None = None
    html: str | None = None
    for part in message.walk():
        if part.is_multipart() or _is_attachment(part):
            continue
        ctype = part.get_content_type()
        if ctype == "text/plain" and plain is None:
            plain = _text_of(part)
        elif ctype == "text/html" and html is None:
            html = _text_of(part)
    if plain and plain.strip():
        return plain.replace("\r\n", "\n").strip()
    if html:
        return html_to_text(html)
    return (plain or "").strip()


@dataclass(frozen=True)
class MailPart:
    """One attachment of a received message."""

    name: str
    mime: str
    data: bytes


def attachments_of(message: Message, *, limit: int = 32) -> list[MailPart]:
    """Attachments (and inline non-text parts), decoded, at most ``limit``."""
    found: list[MailPart] = []
    for part in message.walk():
        if part.is_multipart() or not _is_attachment(part):
            continue
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes) or not payload:
            continue
        raw_name = part.get_filename()
        name = one_line(decode_header_value(raw_name), 200) if raw_name else ""
        mime = part.get_content_type()
        found.append(
            MailPart(name or f"attachment.{part.get_content_subtype() or 'bin'}", mime, payload)
        )
        if len(found) >= limit:
            break
    return found


_MSG_ID = re.compile(r"<[^<>\s]{1,250}>")


def clean_ids(*values: object) -> list[str]:
    """Well-formed ``<id>`` tokens from Message-ID-like headers, in order, unique, last 20."""
    seen: list[str] = []
    for value in values:
        for token in _MSG_ID.findall(str(value or "")):
            if token not in seen:
                seen.append(token)
    return seen[-_MAX_IDS:]


# -- sender authentication -------------------------------------------------------------------------

NO_HEADER = "no Authentication-Results header"
NO_PIN = "no authserv-id is pinned, so Authentication-Results cannot be trusted"
UNTRUSTED = "the topmost Authentication-Results is not from the pinned authserv-id"

_VERDICT = re.compile(r"\s*(dmarc|dkim|spf)\s*=\s*([a-z]+)", re.IGNORECASE)
_QUOTED = r'"(?:[^"\\]|\\.)*"'
_PROPERTY = re.compile(
    rf"([a-z][a-z0-9-]*\.[a-z][a-z0-9-]*)\s*=\s*((?:{_QUOTED}|[^\s\";])+)|(?:{_QUOTED}|[^\s\"]+)",
    re.IGNORECASE,
)


def _clauses(value: str) -> list[str] | None:
    """``value`` split on ``;`` outside quotes and comments (dropped); None when unbalanced."""
    clauses: list[str] = []
    current: list[str] = []
    depth = 0
    quoted = False
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\" and (quoted or depth):
            if quoted:
                current.append(value[index : index + 2])
            index += 2
            continue
        if quoted:
            current.append(char)
            quoted = char != '"'
        elif depth:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if not depth:
                    current.append(" ")
        elif char == "(":
            depth = 1
        elif char == ")":
            return None
        elif char == '"':
            quoted = True
            current.append(char)
        elif char == ";":
            clauses.append("".join(current))
            current = []
        else:
            current.append(char)
        index += 1
    if quoted or depth:
        return None
    clauses.append("".join(current))
    return clauses


def _properties(clause: str) -> list[tuple[str, str]]:
    return [(name.lower(), value.strip('"')) for name, value in _PROPERTY.findall(clause) if name]


def _domain(value: str) -> str:
    return value.rpartition("@")[2].strip().strip(".").lower()


def _aligned(a: str, b: str) -> bool:
    """Relaxed alignment: equal, or one a sub-domain of the other (never a bare TLD)."""
    a, b = _domain(a), _domain(b)
    if not a or not b:
        return False
    if a == b:
        return True
    shorter, longer = sorted((a, b), key=len)
    return "." in shorter and longer.endswith("." + shorter)


def verify_sender(results: Iterable[object], sender: str, *, authserv_id: str) -> tuple[bool, str]:
    """Is ``sender``'s domain authenticated by the receiving server? ``(verdict, reason)``."""
    from_domain = _domain(sender)
    if not from_domain:
        return False, "no From domain"
    headers = [str(h) for h in results]
    if not headers:
        return False, NO_HEADER
    pin = authserv_id.strip().lower()
    if not pin:
        return False, NO_PIN
    topmost = " ".join(_FOLD.sub(" ", headers[0]).split())
    clauses = _clauses(topmost)
    if clauses is None:
        return False, "unbalanced quote or comment in Authentication-Results"
    head = clauses[0].split()
    version_ok = len(head) == 1 or (len(head) == 2 and head[1].isdigit())
    if not head or head[0].lower() != pin or not version_ok:
        return False, UNTRUSTED
    verdicts: dict[str, list[tuple[str, list[tuple[str, str]]]]] = {
        "dmarc": [],
        "spf": [],
        "dkim": [],
    }
    for clause in clauses[1:]:
        match = _VERDICT.match(clause)
        if match is not None:
            verdicts[match.group(1).lower()].append(
                (match.group(2).lower(), _properties(clause[match.end() :]))
            )
    dmarc = verdicts["dmarc"]
    if len(dmarc) > 1:
        return False, "more than one DMARC verdict"
    for result, props in dmarc:
        named = [_domain(v) for p, v in props if p == "header.from"]
        if result == "pass" and all(d == from_domain for d in named):
            return True, "dmarc=pass"
    spf = verdicts["spf"]
    if len(spf) == 1 and spf[0][0] == "pass":
        domains = [v for p, v in spf[0][1] if p in ("smtp.mailfrom", "smtp.from")]
        if domains and all(_aligned(d, from_domain) for d in domains):
            return True, "spf=pass aligned"
    for result, props in verdicts["dkim"]:
        if result != "pass":
            continue
        signers = [v for p, v in props if p == "header.d"]
        if not signers:
            signers = [v for p, v in props if p == "header.i"]
        if signers and all(_aligned(d, from_domain) for d in signers):
            return True, "dkim=pass aligned"
    return False, "not authenticated: " + topmost[:120]
