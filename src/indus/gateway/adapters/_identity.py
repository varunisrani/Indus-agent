"""Identity normalisers shared by several adapters (P16-01).

One person can reach the gateway under several spellings; these helpers turn them into the one
id that authorization and session keys compare.

WhatsApp. A user JID is ``<digits>[:<device>]@<domain>`` with a user domain (``s.whatsapp.net``
for phone numbers, ``lid`` for linked ids, the legacy ``c.us``). Groups (``@g.us``), broadcast
lists and status (``@broadcast``) and channels (``@newsletter``) are chats, never users.
``whatsapp_user_id`` reduces a user JID or a bare phone number to its digits (device suffix,
plus sign and separators dropped) and refuses everything else. The same person can appear as a
phone JID and as a LID; ``WhatsAppIdentities`` keeps those links (from the bridge's mapping files
or from ``link``) and gives every alias the same ``canonical`` id: the phone number when one is
known, else the shortest, then the smallest.

Nostr. Events carry 64-character hex public keys; people write ``npub1…`` (NIP-19, bech32 per
BIP-173). ``npub_to_hex`` decodes with the checksum; ``expand_nostr_allowlist`` adds the hex form
of every npub entry.

Signal. signal-cli names a sender by phone number and by account UUID; the number (when there is
one) is the ``user_id`` and the UUID the ``user_id_alt`` that session keys prefer, so a number
change keeps the conversation.

Phone numbers in logs. ``mask_number`` hides every digit of a number but the last four (separators
kept); ``mask_numbers`` does that to every number-like run in a text (WhatsApp Cloud, SMS).
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

__all__ = [
    "BECH32_CHARSET",
    "WHATSAPP_NON_USER_SUFFIXES",
    "WHATSAPP_USER_DOMAINS",
    "SignalSender",
    "WhatsAppIdentities",
    "bech32_decode",
    "bech32_encode",
    "expand_nostr_allowlist",
    "hex_to_npub",
    "is_whatsapp_broadcast",
    "is_whatsapp_non_user",
    "mask_number",
    "mask_numbers",
    "nostr_pubkey",
    "npub_to_hex",
    "signal_number",
    "signal_sender",
    "signal_uuid",
    "strip_whatsapp_device",
    "to_whatsapp_jid",
    "whatsapp_user_id",
]

# -- WhatsApp ---------------------------------------------------------------------------------

WHATSAPP_USER_DOMAINS = frozenset({"s.whatsapp.net", "lid", "c.us"})
WHATSAPP_NON_USER_SUFFIXES = ("@g.us", "@broadcast", "@newsletter")
_PHONE_DOMAIN = "s.whatsapp.net"

_ASCII_DIGITS = re.compile(r"[0-9]+")
_HUMAN_PHONE = re.compile(r"\+?[0-9][0-9 ().\-]*")
# Mapping files the bridge's auth state keeps: lid-mapping-<id>.json (phone → LID) and
# lid-mapping-<id>_reverse.json (LID → phone).
_MAPPING_FILE = re.compile(r"lid-mapping-([0-9]{1,32})(_reverse)?\.json")
_MAPPING_MAX_BYTES = 4096


def is_whatsapp_non_user(jid: str | None) -> bool:
    """True for group, broadcast and newsletter JIDs: chats, never a sender."""
    text = (jid or "").strip().lower()
    return bool(text) and text.endswith(WHATSAPP_NON_USER_SUFFIXES)


def is_whatsapp_broadcast(jid: str | None) -> bool:
    """Status updates, broadcast lists and channels: never answered."""
    text = (jid or "").strip().lower()
    return bool(text) and text.endswith(("@broadcast", "@newsletter"))


def strip_whatsapp_device(jid: str) -> str:
    """``user:device@domain`` → ``user@domain``; anything without both parts is unchanged."""
    text = jid.strip()
    local, at, domain = text.partition("@")
    if not at or ":" not in local:
        return text
    return f"{local.split(':', 1)[0]}@{domain}"


def _digits_only(text: str) -> str | None:
    return text if _ASCII_DIGITS.fullmatch(text) else None


def whatsapp_user_id(value: str | None) -> str | None:
    """The bare id of a WhatsApp user (digits), or None when ``value`` is not a user."""
    text = (value or "").strip()
    if not text:
        return None
    if "@" in text:
        local, _, domain = text.partition("@")
        if domain.lower() not in WHATSAPP_USER_DOMAINS:
            return None
        local = local.split(":", 1)[0].removeprefix("+")
        return _digits_only(local)
    if _HUMAN_PHONE.fullmatch(text):
        digits = re.sub(r"[^0-9]", "", text)
        return digits or None
    return None


def to_whatsapp_jid(target: str) -> str:
    """An outbound target as a JID the bridge accepts: a bare phone number (separators allowed)
    becomes ``<digits>@s.whatsapp.net``, a device suffix is dropped, other JIDs (groups) are kept.
    Empty input gives ``""``; anything else unrecognised is returned as given for the bridge to
    refuse."""
    text = target.strip()
    if not text:
        return ""
    if "@" in text:
        return strip_whatsapp_device(text)
    if _HUMAN_PHONE.fullmatch(text):
        digits = re.sub(r"[^0-9]", "", text)
        if digits:
            return f"{digits}@{_PHONE_DOMAIN}"
    return text


class WhatsAppIdentities:
    """Alias links between a person's phone id and LID (see the module notes).

    Ids are kept bare (``whatsapp_user_id``). Links are transitive: linking A–B and B–C makes all
    three one person. Thread-free; the owning adapter calls it from its event loop."""

    def __init__(self) -> None:
        self._group: dict[str, set[str]] = {}
        self._phones: set[str] = set()

    def link(self, phone: str, lid: str) -> bool:
        """Record that ``phone`` and ``lid`` are one person; False when either is not a user."""
        left, right = whatsapp_user_id(phone), whatsapp_user_id(lid)
        if left is None or right is None:
            return False
        self._phones.add(left)
        merged = self._group.get(left, {left}) | self._group.get(right, {right})
        for member in merged:
            self._group[member] = merged
        return True

    def load_mapping_dir(self, directory: Path) -> int:
        """Read the bridge's ``lid-mapping-*.json`` files in ``directory``; returns the number of
        links taken. Unreadable, oversized or oddly named files are skipped."""
        taken = 0
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            return 0
        for path in entries:
            match = _MAPPING_FILE.fullmatch(path.name)
            if match is None:
                continue
            try:
                if not path.is_file() or path.stat().st_size > _MAPPING_MAX_BYTES:
                    continue
                mapped = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError, UnicodeDecodeError) as exc:
                log.debug("skipping WhatsApp mapping file %s: %s", path.name, type(exc).__name__)
                continue
            if not isinstance(mapped, str | int) or isinstance(mapped, bool):
                continue
            named, other = match.group(1), str(mapped)
            reverse = match.group(2) is not None
            phone, lid = (other, named) if reverse else (named, other)
            if self.link(phone, lid):
                taken += 1
        return taken

    def aliases(self, value: str | None) -> frozenset[str]:
        """Every bare id known for this person (the id itself included); empty for non-users."""
        bare = whatsapp_user_id(value)
        if bare is None:
            return frozenset()
        return frozenset(self._group.get(bare, {bare}))

    def canonical(self, value: str | None) -> str | None:
        """The one id for this person (see the module notes); None for non-users."""
        members = self.aliases(value)
        if not members:
            return None
        pool = (members & self._phones) or members
        return min(pool, key=lambda m: (len(m), m))

    def same_user(self, left: str | None, right: str | None) -> bool:
        a, b = self.aliases(left), self.aliases(right)
        return bool(a and b and a & b)

    def allowlist_matches(self, candidate: str | None, entries: Iterable[str]) -> bool:
        """True when ``candidate`` (a user) is on ``entries`` under any alias; ``*`` allows
        every user. Group and broadcast JIDs never match."""
        mine = self.aliases(candidate)
        if not mine:
            return False
        for entry in entries:
            text = str(entry).strip()
            if text == "*":
                return True
            if self.aliases(text) & mine:
                return True
        return False


# -- Nostr (NIP-19 over BIP-173 bech32) -------------------------------------------------------

BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32_VALUES = {ch: n for n, ch in enumerate(BECH32_CHARSET)}
_BECH32_GEN = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
# NIP-19 strings with TLV data can exceed BIP-173's 90 characters; this only bounds the work.
_BECH32_MAX_LEN = 5000


def _polymod(values: Iterable[int]) -> int:
    check = 1
    for value in values:
        top = check >> 25
        check = ((check & 0x1FFFFFF) << 5) ^ value
        for bit, gen in enumerate(_BECH32_GEN):
            if (top >> bit) & 1:
                check ^= gen
    return check


def _hrp_values(hrp: str) -> list[int]:
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def _regroup(data: Iterable[int], source: int, target: int, *, pad: bool) -> list[int] | None:
    """Re-pack ``source``-bit groups into ``target``-bit groups (None on invalid input)."""
    acc = 0
    held = 0
    mask = (1 << target) - 1
    out: list[int] = []
    for value in data:
        if value < 0 or value >> source:
            return None
        acc = (acc << source) | value
        held += source
        while held >= target:
            held -= target
            out.append((acc >> held) & mask)
    if pad:
        if held:
            out.append((acc << (target - held)) & mask)
    elif held >= source or (acc << (target - held)) & mask:
        return None
    return out


def bech32_decode(text: str) -> tuple[str, bytes] | None:
    """``(hrp, payload bytes)`` of a valid bech32 string, else None (mixed case, bad checksum,
    unknown characters, bad padding)."""
    if not text or len(text) > _BECH32_MAX_LEN:
        return None
    if text.lower() != text and text.upper() != text:
        return None
    lowered = text.lower()
    hrp, sep, rest = lowered.rpartition("1")
    if not sep or not hrp or len(rest) < 6:
        return None
    if any(not 33 <= ord(c) <= 126 for c in hrp):
        return None
    try:
        values = [_BECH32_VALUES[c] for c in rest]
    except KeyError:
        return None
    if _polymod(_hrp_values(hrp) + values) != 1:
        return None
    payload = _regroup(values[:-6], 5, 8, pad=False)
    if payload is None:
        return None
    return hrp, bytes(payload)


def bech32_encode(hrp: str, payload: bytes) -> str:
    """The bech32 string for ``payload`` under ``hrp`` (lower case)."""
    hrp = hrp.lower()
    values = _regroup(payload, 8, 5, pad=True)
    if values is None:  # pragma: no cover - bytes are always 0..255
        raise ValueError("payload is not bytes")
    check = _polymod([*_hrp_values(hrp), *values, 0, 0, 0, 0, 0, 0]) ^ 1
    tail = [(check >> 5 * (5 - n)) & 31 for n in range(6)]
    return hrp + "1" + "".join(BECH32_CHARSET[v] for v in (*values, *tail))


_HEX_KEY = re.compile(r"[0-9a-fA-F]{64}")


def npub_to_hex(value: str | None) -> str | None:
    """The 64-character hex public key of an ``npub1…`` string, else None."""
    decoded = bech32_decode((value or "").strip())
    if decoded is None:
        return None
    hrp, payload = decoded
    if hrp != "npub" or len(payload) != 32:
        return None
    return payload.hex()


def hex_to_npub(value: str) -> str:
    """``npub1…`` for a 64-character hex public key (``ValueError`` otherwise)."""
    text = value.strip()
    if not _HEX_KEY.fullmatch(text):
        raise ValueError("a Nostr public key is 64 hex characters")
    return bech32_encode("npub", bytes.fromhex(text))


def nostr_pubkey(value: str | None) -> str | None:
    """Lower-case hex for a hex key or an npub; None for anything else."""
    text = (value or "").strip()
    if _HEX_KEY.fullmatch(text):
        return text.lower()
    return npub_to_hex(text)


def expand_nostr_allowlist(entries: Iterable[str]) -> frozenset[str]:
    """The entries as given (hex lower-cased) plus the hex form of every valid npub, so an npub
    entry authorises the hex pubkey events carry. Invalid entries stay and never match."""
    out: set[str] = set()
    for entry in entries:
        text = str(entry).strip()
        if not text:
            continue
        out.add(text.lower() if _HEX_KEY.fullmatch(text) else text)
        decoded = npub_to_hex(text)
        if decoded is not None:
            out.add(decoded)
    return frozenset(out)


# -- Signal -----------------------------------------------------------------------------------

_E164 = re.compile(r"\+[1-9][0-9]{6,14}")


@dataclass(frozen=True)
class SignalSender:
    user_id: str
    user_id_alt: str | None = None


def signal_number(value: str | None) -> str | None:
    """An E.164 number (``+`` and digits; spaces, dots, dashes and brackets dropped), or None."""
    text = (value or "").strip()
    if not text.startswith("+"):
        return None
    compact = re.sub(r"[ ().\-]", "", text)
    return compact if _E164.fullmatch(compact) else None


def signal_uuid(value: str | None) -> str | None:
    """A Signal account UUID in canonical lower-case form, or None."""
    text = (value or "").strip()
    if len(text) != 36:
        return None
    try:
        parsed = uuid.UUID(text)
    except ValueError:
        return None
    canonical = str(parsed)
    return canonical if canonical == text.lower() else None


def signal_sender(number: str | None, account_uuid: str | None) -> SignalSender | None:
    """The sender ids of a signal-cli envelope: the number when there is one, else the UUID; the
    UUID is always the alternate id. None when neither is usable."""
    phone = signal_number(number)
    ident = signal_uuid(account_uuid)
    if phone is not None:
        return SignalSender(phone, ident)
    if ident is not None:
        return SignalSender(ident, ident)
    return None


# -- phone numbers in logs --------------------------------------------------------------------

_NUMBER_RUN = re.compile(r"\+?\d[\d ().-]{6,}\d")


def mask_number(value: str) -> str:
    """A phone number for logs: every digit but the last four hidden."""
    digits = sum(c.isdigit() for c in value)
    keep = 4 if digits > 6 else 0
    out: list[str] = []
    seen = 0
    for char in value:
        if char.isdigit():
            seen += 1
            out.append(char if seen > digits - keep else "*")
        else:
            out.append(char)
    return "".join(out)


def mask_numbers(text: str) -> str:
    """``text`` with every phone-number-like run masked."""
    return _NUMBER_RUN.sub(lambda m: mask_number(m.group(0)), text)
