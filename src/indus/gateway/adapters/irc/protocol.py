"""IRC line handling for the IRC adapter (P16-27): parsing, building and splitting lines.

A line is ``[@tags] [:prefix] COMMAND params… [:trailing]`` (RFC 1459/2812, IRCv3 message-tags),
at most 512 bytes with its CRLF. Tag values are unescaped (``\\:`` → ``;``, ``\\s`` → space,
``\\\\`` → ``\\``, ``\\r``, ``\\n``; a trailing lone backslash is dropped).

Outbound text: CR and NUL never reach the wire (they could start a second command); line breaks
split the text into separate lines; each payload is at most ``payload_budget(target)`` UTF-8 bytes
— 450, or less when ``PRIVMSG <target> :`` and CRLF leave less of the 512 — and is cut on
character boundaries, preferring the last space. `split_payload` returns each piece with its
offset in the cleaned text, so a send that stopped part way can resume from the first piece that
did not land.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

__all__ = [
    "LINE_MAX_BYTES",
    "PAYLOAD_MAX_BYTES",
    "IrcLine",
    "clean_text",
    "format_line",
    "is_channel",
    "payload_budget",
    "sasl_plain_chunks",
    "split_payload",
    "utf8_bytes",
    "valid_target",
]

LINE_MAX_BYTES: Final = 512
PAYLOAD_MAX_BYTES: Final = 450
SASL_CHUNK: Final = 400
_TAG_ESCAPES: Final = {":": ";", "s": " ", "\\": "\\", "r": "\r", "n": "\n"}
_FORBIDDEN_IN_TARGET: Final = frozenset(" ,\r\n\x00\x07")


def utf8_bytes(text: str) -> int:
    return len(text.encode("utf-8", "surrogatepass"))


def _unescape(value: str) -> str:
    out: list[str] = []
    chars = iter(value)
    for char in chars:
        if char != "\\":
            out.append(char)
            continue
        nxt = next(chars, "")
        out.append(_TAG_ESCAPES.get(nxt, nxt))
    return "".join(out)


@dataclass(frozen=True)
class IrcLine:
    command: str
    params: tuple[str, ...] = ()
    prefix: str = ""
    tags: Mapping[str, str] = field(default_factory=dict[str, str])

    @property
    def nick(self) -> str:
        """The nick of the prefix (``nick!user@host``), or the server name."""
        return self.prefix.split("!", 1)[0].split("@", 1)[0]

    def param(self, index: int) -> str:
        return self.params[index] if -len(self.params) <= index < len(self.params) else ""

    @classmethod
    def parse(cls, raw: str) -> IrcLine | None:
        """One received line (without CRLF); None when it has no command."""
        rest = raw.rstrip("\r\n")
        tags: dict[str, str] = {}
        if rest.startswith("@"):
            block, _, rest = rest[1:].partition(" ")
            for item in block.split(";"):
                if not item:
                    continue
                key, _, value = item.partition("=")
                tags[key] = _unescape(value)
            rest = rest.lstrip(" ")
        prefix = ""
        if rest.startswith(":"):
            prefix, _, rest = rest[1:].partition(" ")
            rest = rest.lstrip(" ")
        trailing: str | None = None
        if " :" in rest:
            rest, trailing = rest.split(" :", 1)
        elif rest.startswith(":"):
            rest, trailing = "", rest[1:]
        words = rest.split()
        if not words:
            return None
        params = [*words[1:], *([trailing] if trailing is not None else [])]
        return cls(words[0].upper(), tuple(params), prefix, tags)


def format_line(command: str, *params: str) -> str:
    """A line to send; the last parameter becomes the trailing one when it needs to be.

    ``ValueError`` for a CR, LF or NUL anywhere, or a space or leading ``:`` in a middle
    parameter (either would change which command the server reads)."""
    parts = [command]
    for n, param in enumerate(params):
        if any(c in param for c in "\r\n\x00"):
            raise ValueError("IRC parameters cannot hold CR, LF or NUL")
        last = n == len(params) - 1
        if last and (not param or " " in param or param.startswith(":")):
            parts.append(":" + param)
        elif " " in param or param.startswith(":") or not param:
            raise ValueError(f"IRC middle parameter {param!r} is not allowed")
        else:
            parts.append(param)
    return " ".join(parts)


def is_channel(target: str) -> bool:
    return target[:1] in ("#", "&", "+", "!") and len(target) > 1


def valid_target(target: str) -> bool:
    """A nick or channel we may address: no spaces, commas, control characters or ``:``."""
    return (
        bool(target)
        and len(target) <= 200
        and not target.startswith(":")
        and not any(c in _FORBIDDEN_IN_TARGET for c in target)
    )


def payload_budget(target: str) -> int:
    """Payload bytes of one PRIVMSG to ``target``: 450, or what the 512-byte line leaves."""
    overhead = utf8_bytes(f"PRIVMSG {target} :") + 2
    return max(1, min(PAYLOAD_MAX_BYTES, LINE_MAX_BYTES - overhead))


def clean_text(text: str) -> str:
    """Line breaks normalised to LF, NUL removed."""
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")


def _fit(line: str, budget: int) -> int:
    """Characters of ``line`` that fit ``budget`` UTF-8 bytes (at least one)."""
    used = 0
    for index, char in enumerate(line):
        used += utf8_bytes(char)
        if used > budget:
            return max(index, 1)
    return len(line)


def split_payload(text: str, budget: int) -> list[tuple[str, int]]:
    """``(payload, offset)`` pieces of ``text`` (already cleaned) for one target.

    Each line of the text is its own piece or pieces; blank lines are skipped. A line over the
    budget is cut at the last space inside the budget when that keeps at least a third of it,
    else at the last whole character that fits."""
    pieces: list[tuple[str, int]] = []
    start = 0
    for line in text.split("\n"):
        offset = start
        start += len(line) + 1
        rest = line
        while rest.strip():
            lead = len(rest) - len(rest.lstrip(" "))
            rest, offset = rest[lead:], offset + lead
            if utf8_bytes(rest.rstrip()) <= budget:
                pieces.append((rest.rstrip(), offset))
                break
            cut = _fit(rest, budget)
            space = rest.rfind(" ", 0, cut)
            if space > cut // 3:
                cut = space
            pieces.append((rest[:cut].rstrip(), offset))
            rest, offset = rest[cut:], offset + cut
    return pieces


def sasl_plain_chunks(username: str, password: str) -> list[str]:
    """``AUTHENTICATE`` payloads for SASL PLAIN: base64 of ``user\\0user\\0password`` in 400-byte
    pieces, with a final ``+`` when the last piece is exactly 400 bytes."""
    raw = f"{username}\x00{username}\x00{password}".encode()
    encoded = base64.b64encode(raw).decode()
    chunks = [encoded[i : i + SASL_CHUNK] for i in range(0, len(encoded), SASL_CHUNK)] or ["+"]
    if len(chunks[-1]) == SASL_CHUNK:
        chunks.append("+")
    return chunks
