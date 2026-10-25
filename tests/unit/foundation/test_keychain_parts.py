"""Keychain values longer than one entry may hold (Windows Credential Manager: 1280 UTF-16 code
units) are kept in parts behind a head entry, so a provider sign-in's OAuth tokens fit."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from indus.foundation.errors import SecretStoreError
from indus.foundation.secrets import (
    CHUNK_MARK,
    SERVICE,
    WINDOWS_MAX_UNITS,
    KeychainBackend,
    SecretStore,
)

PROFILE = "p1"


class CappedKeyring:
    """A keyring that refuses entries over the Windows cap, as ``CredWrite`` does (error 1783)."""

    def __init__(self, cap: int = WINDOWS_MAX_UNITS) -> None:
        self.cap = cap
        self.entries: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str, /) -> str | None:
        return self.entries.get((service, username))

    def set_password(self, service: str, username: str, password: str, /) -> None:
        if len(password.encode("utf-16-le")) // 2 > self.cap:
            raise OSError(1783, "CredWrite", "The stub received bad data")
        self.entries[(service, username)] = password

    def delete_password(self, service: str, username: str, /) -> None:
        if self.entries.pop((service, username), None) is None:
            raise KeyError(username)

    def accounts(self) -> set[str]:
        return {account for service, account in self.entries if service == SERVICE}


def sign_in_bundle() -> str:
    """OAuth tokens as the sign-in keeps them: well over one Windows entry."""
    tokens = {
        "access_token": "a" * 1900,
        "refresh_token": "r" * 700,
        "id_token": "i" * 1300,
        "token_type": "Bearer",
    }
    return json.dumps(tokens, sort_keys=True)


def backend(keyring: CappedKeyring) -> KeychainBackend:
    return KeychainBackend(keyring, PROFILE, max_units=WINDOWS_MAX_UNITS)


def test_a_long_value_is_kept_in_parts_and_read_back_whole() -> None:
    keyring = CappedKeyring()
    store = backend(keyring)
    value = sign_in_bundle()
    store.set("OPENAI_SIGNIN_1", value)
    head = keyring.entries[(SERVICE, f"{PROFILE}:OPENAI_SIGNIN_1")]
    assert head.startswith(CHUNK_MARK) and value not in head
    assert len(keyring.accounts()) == 1 + int(head.removeprefix(CHUNK_MARK).split(":")[0])
    assert store.get("OPENAI_SIGNIN_1") == value


def test_a_value_that_fits_is_one_entry_as_before() -> None:
    keyring = CappedKeyring()
    store = backend(keyring)
    store.set("OPENAI_API_KEY", "k" * WINDOWS_MAX_UNITS)
    assert keyring.accounts() == {f"{PROFILE}:OPENAI_API_KEY"}
    assert store.get("OPENAI_API_KEY") == "k" * WINDOWS_MAX_UNITS


def test_parts_never_split_a_character() -> None:
    keyring = CappedKeyring()
    store = backend(keyring)
    value = "\U0001f600" * 1000 + "é" * 500  # 2000 + 500 UTF-16 units
    store.set("EMOJI", value)
    assert store.get("EMOJI") == value


def test_shrinking_or_deleting_leaves_no_parts_behind() -> None:
    keyring = CappedKeyring()
    store = backend(keyring)
    store.set("TOKEN", sign_in_bundle())
    store.set("TOKEN", "short")
    assert keyring.accounts() == {f"{PROFILE}:TOKEN"}
    assert store.get("TOKEN") == "short"
    store.set("TOKEN", sign_in_bundle())
    assert store.delete("TOKEN") is True
    assert keyring.accounts() == set()
    assert store.delete("TOKEN") is False


def test_a_value_that_looks_like_a_head_is_kept_in_parts() -> None:
    keyring = CappedKeyring()
    store = backend(keyring)
    tricky = f"{CHUNK_MARK}9:deadbeef"
    store.set("ODD", tricky)
    assert store.get("ODD") == tricky


def test_missing_or_tampered_parts_are_an_error_not_a_wrong_secret() -> None:
    keyring = CappedKeyring()
    store = backend(keyring)
    store.set("TOKEN", sign_in_bundle())
    keyring.entries[(SERVICE, f"{PROFILE}:TOKEN#2")] = "x"
    with pytest.raises(SecretStoreError, match="incomplete"):
        store.get("TOKEN")
    del keyring.entries[(SERVICE, f"{PROFILE}:TOKEN#2")]
    with pytest.raises(SecretStoreError, match="incomplete"):
        store.get("TOKEN")


def test_without_a_cap_nothing_changes() -> None:
    keyring = CappedKeyring(cap=10**9)
    store = KeychainBackend(keyring, PROFILE)
    value = sign_in_bundle()
    store.set("TOKEN", value)
    assert keyring.entries[(SERVICE, f"{PROFILE}:TOKEN")] == value


def test_the_store_saves_a_sign_in_through_a_capped_keychain(tmp_path: Path) -> None:
    keyring = CappedKeyring()
    store = SecretStore(backend(keyring), tmp_path / "secrets.index.json")
    value = sign_in_bundle()
    store.set("OPENAI_SIGNIN_1", value)
    assert store.names() == ["OPENAI_SIGNIN_1"]  # parts never show up as names
    fresh = SecretStore(backend(keyring), tmp_path / "secrets.index.json")
    assert fresh.get("OPENAI_SIGNIN_1") == value
