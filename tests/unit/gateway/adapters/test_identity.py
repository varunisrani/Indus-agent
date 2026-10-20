"""Identity normalisers: WhatsApp JID/LID, Nostr npub, Signal number and UUID (P16-01)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from indus.gateway.adapters._identity import (
    SignalSender,
    WhatsAppIdentities,
    bech32_decode,
    bech32_encode,
    expand_nostr_allowlist,
    hex_to_npub,
    is_whatsapp_broadcast,
    is_whatsapp_non_user,
    nostr_pubkey,
    npub_to_hex,
    signal_number,
    signal_sender,
    signal_uuid,
    strip_whatsapp_device,
    to_whatsapp_jid,
    whatsapp_user_id,
)

VECTORS: dict[str, Any] = json.loads(
    (Path(__file__).parents[3] / "fixtures" / "gateway" / "nip19_vectors.json").read_text()
)
PHONE = "15551230001"
LID = "204812345678901"

# -- WhatsApp -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (f"{PHONE}@s.whatsapp.net", PHONE),
        (f"{PHONE}:12@s.whatsapp.net", PHONE),
        (f"+{PHONE}@s.whatsapp.net", PHONE),
        (f"{LID}@lid", LID),
        (f"{LID}:3@lid", LID),
        (f"{PHONE}@c.us", PHONE),
        (f"{PHONE}@S.WhatsApp.Net", PHONE),
        (f"  {PHONE}@s.whatsapp.net ", PHONE),
        ("+1 (555) 123-0001", PHONE),
        (PHONE, PHONE),
        ("120363012345678901@g.us", None),
        ("status@broadcast", None),
        ("1555@broadcast", None),
        ("120363000000000000@newsletter", None),
        ("1555@example.com", None),
        ("abc@s.whatsapp.net", None),
        ("１５５５@s.whatsapp.net", None),  # full-width digits are not ASCII digits
        ("../etc@lid", None),
        ("", None),
        (None, None),
        ("   ", None),
        ("call me", None),
    ],
)
def test_whatsapp_user_ids(value: str | None, expected: str | None) -> None:
    assert whatsapp_user_id(value) == expected


def test_non_user_and_broadcast_jids() -> None:
    for jid in ("120363@g.us", "status@broadcast", "99@broadcast", "123@newsletter", "X@G.US"):
        assert is_whatsapp_non_user(jid)
    for jid in (f"{PHONE}@s.whatsapp.net", f"{LID}@lid", PHONE, "", None):
        assert not is_whatsapp_non_user(jid)
    assert is_whatsapp_broadcast("status@broadcast") and is_whatsapp_broadcast("5@newsletter")
    assert not is_whatsapp_broadcast("120363@g.us") and not is_whatsapp_broadcast(None)


def test_device_suffix_and_outbound_jids() -> None:
    assert strip_whatsapp_device(f"{PHONE}:7@s.whatsapp.net") == f"{PHONE}@s.whatsapp.net"
    assert strip_whatsapp_device(f"{LID}:0@lid") == f"{LID}@lid"
    assert strip_whatsapp_device("120363@g.us") == "120363@g.us"
    assert strip_whatsapp_device("no-at:1") == "no-at:1"
    assert to_whatsapp_jid("+1 555-123-0001") == f"{PHONE}@s.whatsapp.net"
    assert to_whatsapp_jid(f"{PHONE}:4@s.whatsapp.net") == f"{PHONE}@s.whatsapp.net"
    assert to_whatsapp_jid("120363@g.us") == "120363@g.us"
    assert to_whatsapp_jid("  ") == ""
    assert to_whatsapp_jid("somebody") == "somebody"  # the bridge refuses it with a real error


def test_a_jid_with_a_device_suffix_and_its_lid_alias_are_one_person() -> None:
    book = WhatsAppIdentities()
    assert book.link(f"{PHONE}@s.whatsapp.net", f"{LID}@lid")
    forms = (f"{PHONE}:12@s.whatsapp.net", f"{LID}:3@lid", f"{LID}@lid", f"+{PHONE}", PHONE)
    assert {book.canonical(f) for f in forms} == {PHONE}
    assert book.aliases(f"{LID}:9@lid") == {PHONE, LID}
    assert book.same_user(f"{PHONE}:12@s.whatsapp.net", f"{LID}@lid")
    assert not book.same_user(f"{PHONE}@s.whatsapp.net", "15559990000@s.whatsapp.net")


def test_groups_and_broadcasts_never_join_an_alias_book() -> None:
    book = WhatsAppIdentities()
    assert not book.link("120363@g.us", f"{LID}@lid")
    assert not book.link(PHONE, "status@broadcast")
    assert book.canonical("120363@g.us") is None
    assert book.aliases("status@broadcast") == frozenset()
    assert not book.same_user("120363@g.us", "120363@g.us")
    assert not book.allowlist_matches("120363@g.us", ["*", "120363@g.us"])


def test_links_are_transitive_and_the_phone_wins() -> None:
    book = WhatsAppIdentities()
    book.link(PHONE, f"{LID}@lid")
    book.link("15551230002", "2048")  # another person
    book.link("999", f"{LID}@lid")  # a second phone for the first person (re-registered)
    assert book.aliases(LID) == {PHONE, LID, "999"}
    assert book.canonical(LID) == "999"  # phones first, then the shortest
    assert book.canonical("2048@lid") == "15551230002"
    unknown = WhatsAppIdentities()
    assert unknown.canonical(f"{LID}:1@lid") == LID  # no link: the id itself


def test_allowlists_match_across_aliases() -> None:
    book = WhatsAppIdentities()
    book.link(PHONE, LID)
    assert book.allowlist_matches(f"{LID}@lid", [f"+{PHONE}"])
    assert book.allowlist_matches(f"{PHONE}:2@s.whatsapp.net", [f"{LID}@lid"])
    assert book.allowlist_matches("44123@s.whatsapp.net", ["*"])
    assert not book.allowlist_matches("44123@s.whatsapp.net", [PHONE, "garbage", ""])
    assert not book.allowlist_matches(None, ["*"])


def test_mapping_files_from_the_bridge_auth_state(tmp_path: Path) -> None:
    (tmp_path / f"lid-mapping-{PHONE}.json").write_text(json.dumps(LID))
    (tmp_path / "lid-mapping-2048_reverse.json").write_text("﻿" + json.dumps("15551230002"))
    (tmp_path / "lid-mapping-77.json").write_text(json.dumps(88))
    (tmp_path / "lid-mapping-55.json").write_text("{not json")
    (tmp_path / "lid-mapping-66.json").write_text(json.dumps(True))
    (tmp_path / "lid-mapping-..json").write_text(json.dumps("1"))
    (tmp_path / "lid-mapping-44.json").write_text(json.dumps("4" * 5000))
    (tmp_path / "creds.json").write_text(json.dumps("12345"))
    (tmp_path / "lid-mapping-33.json").mkdir()
    book = WhatsAppIdentities()
    assert book.load_mapping_dir(tmp_path) == 3
    assert book.same_user(f"{PHONE}@s.whatsapp.net", f"{LID}@lid")
    assert book.canonical("2048@lid") == "15551230002"
    assert book.same_user("77", "88")
    assert book.aliases("44") == {"44"}  # the oversized file was skipped
    assert WhatsAppIdentities().load_mapping_dir(tmp_path / "missing") == 0


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs POSIX permissions")
def test_an_unreadable_mapping_file_is_skipped(tmp_path: Path) -> None:
    locked = tmp_path / "lid-mapping-12.json"
    locked.write_text(json.dumps("34"))
    locked.chmod(0)
    assert WhatsAppIdentities().load_mapping_dir(tmp_path) == 0


# -- Nostr --------------------------------------------------------------------------------------


@pytest.mark.parametrize("vector", VECTORS["nip19"], ids=lambda v: v["hrp"])
def test_nip19_vectors_decode_and_encode(vector: dict[str, str]) -> None:
    decoded = bech32_decode(vector["bech32"])
    assert decoded is not None and decoded[0] == vector["hrp"]
    assert decoded[1].hex() == vector["hex"]
    assert bech32_encode(vector["hrp"], bytes.fromhex(vector["hex"])) == vector["bech32"]
    assert npub_to_hex(vector["bech32"]) == vector["hex"]
    assert hex_to_npub(vector["hex"]) == vector["bech32"]
    assert hex_to_npub(vector["hex"].upper()) == vector["bech32"]


@pytest.mark.parametrize("text", VECTORS["bip173_valid"])
def test_bip173_valid_strings(text: str) -> None:
    decoded = bech32_decode(text)
    assert decoded is not None
    hrp, payload = decoded
    assert bech32_encode(hrp, payload) == text.lower()


@pytest.mark.parametrize("text", VECTORS["bip173_invalid"])
def test_bip173_invalid_strings(text: str) -> None:
    assert bech32_decode(text) is None


def test_npub_decoding_refuses_everything_else() -> None:
    good = VECTORS["nip19"][0]["bech32"]
    assert npub_to_hex(good.upper()) == VECTORS["nip19"][0]["hex"]
    assert npub_to_hex("  " + good + "\n") == VECTORS["nip19"][0]["hex"]
    flipped = good[:-1] + ("q" if good[-1] != "q" else "p")
    assert npub_to_hex(flipped) is None  # checksum
    assert npub_to_hex(good[:10] + good[10:].upper()) is None  # mixed case
    assert npub_to_hex(good.replace("0", "b", 1)) is None  # "b" is not in the charset
    assert npub_to_hex(bech32_encode("nsec", bytes(32))) is None  # wrong hrp
    assert npub_to_hex(bech32_encode("npub", bytes(31))) is None  # wrong length
    assert npub_to_hex("npub1") is None and npub_to_hex("") is None and npub_to_hex(None) is None
    assert npub_to_hex("npub1" + "q" * 6000) is None
    with pytest.raises(ValueError):
        hex_to_npub("abc")


def test_nostr_allowlists_authorise_the_hex_pubkey() -> None:
    vector = VECTORS["nip19"][0]
    allowed = expand_nostr_allowlist([vector["bech32"], "  ", "npub1broken", "F" * 64, "*"])
    assert vector["hex"] in allowed and vector["bech32"] in allowed
    assert "f" * 64 in allowed and "npub1broken" in allowed and "*" in allowed
    assert "" not in allowed
    assert nostr_pubkey(vector["hex"].upper()) == vector["hex"]
    assert nostr_pubkey(vector["bech32"]) == vector["hex"]
    assert nostr_pubkey("not a key") is None and nostr_pubkey(None) is None


# -- Signal -------------------------------------------------------------------------------------

UUID = "5f8b1a2c-3d4e-4f60-8a9b-0c1d2e3f4a5b"


def test_signal_numbers_and_uuids() -> None:
    assert signal_number("+1 (555) 123-0001") == "+15551230001"
    assert signal_number("+44.20.7946.0018") == "+442079460018"
    for bad in ("15551230001", "+0123456789", "+12", "+1555abc0001", "", None):
        assert signal_number(bad) is None
    assert signal_uuid(UUID.upper()) == UUID
    for bad in (UUID.replace("-", ""), "{" + UUID + "}", UUID + "0", "PNI:" + UUID, "", None):
        assert signal_uuid(bad) is None


def test_signal_senders_keep_the_uuid_as_the_alternate_id() -> None:
    assert signal_sender("+15551230001", UUID) == SignalSender("+15551230001", UUID)
    assert signal_sender("+15551230001", None) == SignalSender("+15551230001", None)
    assert signal_sender(None, UUID.upper()) == SignalSender(UUID, UUID)
    assert signal_sender("garbage", UUID) == SignalSender(UUID, UUID)
    assert signal_sender(None, "not-a-uuid") is None
