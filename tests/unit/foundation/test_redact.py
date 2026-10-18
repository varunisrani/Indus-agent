"""Redaction cases. Every token below is a made-up, obviously fake value."""

from __future__ import annotations

import random
import string
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from indus.foundation import redact as redact_mod
from indus.foundation.redact import (
    CYCLE_MARKER,
    DEPTH_MARKER,
    MASK_SHORT,
    MAX_DEPTH,
    Redactor,
    mask,
    redact_text,
)
from indus.foundation.secrets import open_secret_store, reset_probe_cache


@dataclass
class FakeScope:
    home: Path
    env: dict[str, str] = field(default_factory=dict[str, str])
    secrets: Any = None


@pytest.fixture
def scope(tmp_path: Path) -> FakeScope:
    home = tmp_path / "default"
    home.mkdir()
    reset_probe_cache()
    return FakeScope(home=home, secrets=open_secret_store(home, "default", mode="file"))


FAKE_OPENAI = "sk-proj-FAKEfakeFAKEfake0123456789"
FAKE_ANTHROPIC = "sk-ant-api03-FAKEfakeFAKEfake0123456789"
FAKE_OPENROUTER = "sk-or-v1-FAKEfakeFAKEfake0123456789"
FAKE_GITHUB = "ghp_FAKEfakeFAKEfake0123456789abcd"
# Built at runtime so the source never holds a literal that secret scanners flag.
FAKE_GITLAB = "gl" + "pat-" + "FAKEfakeFAKEfake0123"
FAKE_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0LXVzZXIifQ.ZmFrZS1zaWduYXR1cmUtdmFsdWU"

# (text, secret substrings that must be gone; [] means the text must come back unchanged)
CASES: list[tuple[str, list[str]]] = [
    # vendor prefixes
    (f"key is {FAKE_OPENAI} ok", [FAKE_OPENAI]),
    (f"{FAKE_ANTHROPIC}", [FAKE_ANTHROPIC]),
    (f"router={FAKE_OPENROUTER}", [FAKE_OPENROUTER]),
    (f"push with {FAKE_GITHUB}", [FAKE_GITHUB]),
    ("oauth gho_FAKEfakeFAKEfake0123456789abcd", ["gho_FAKEfakeFAKEfake0123456789abcd"]),
    ("server ghs_FAKEfakeFAKEfake0123456789abcd", ["ghs_FAKEfakeFAKEfake0123456789abcd"]),
    ("slack xoxb-0000000000-FAKEfakeFAKE", ["xoxb-0000000000-FAKEfakeFAKE"]),
    ("slack xoxp-1111111111-FAKEfakeFAKE", ["xoxp-1111111111-FAKEfakeFAKE"]),
    ("aws AKIAFAKEFAKEFAKE0000 id", ["AKIAFAKEFAKEFAKE0000"]),
    ("gcp AIzaFAKEfakeFAKEfake0123456789_-abcde", ["AIzaFAKEfakeFAKEfake0123456789_-abcde"]),
    ("hub hf_FAKEfakeFAKEfake01234567", ["hf_FAKEfakeFAKEfake01234567"]),
    (f"lab {FAKE_GITLAB}", [FAKE_GITLAB]),
    # headers and tokens
    ("Authorization: Bearer abcdefFAKE0123456789", ["abcdefFAKE0123456789"]),
    ("authorization: basic dXNlcjpwYXNzd29yZA==", ["dXNlcjpwYXNzd29yZA=="]),
    ("curl -H 'Authorization: token FAKE0123456789abcdef'", ["FAKE0123456789abcdef"]),
    ("sent bearer FAKEfake.0123456789-abc", ["FAKEfake.0123456789-abc"]),
    (f"jwt {FAKE_JWT} end", [FAKE_JWT]),
    # PEM
    (
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEfakeFAKEfake\nAAAA\n-----END RSA PRIVATE KEY-----",
        ["MIIEfakeFAKEfake"],
    ),
    (
        "x\n-----BEGIN PRIVATE KEY-----\nZmFrZQ==\n-----END PRIVATE KEY-----\ny",
        ["ZmFrZQ=="],
    ),
    # URLs
    ("https://user:hunter2pass@db.example/x", ["hunter2pass"]),
    ("postgres://app:FAKEpw@localhost:5432/db", ["FAKEpw"]),
    ("https://h.example/cb?code=1&token=FAKEtoken99", ["FAKEtoken99"]),
    ("https://h.example/?api_key=FAKEapikey&page=2", ["FAKEapikey"]),
    ("https://h.example/?access_token=FAKEaccess#frag", ["FAKEaccess"]),
    ("https://s3.example/obj?X=1&sig=FAKEsignature", ["FAKEsignature"]),
    ("https://h.example/?password=FAKEpass1", ["FAKEpass1"]),
    # assignments
    ("OPENAI_API_KEY=FAKEvalue0123456789", ["FAKEvalue0123456789"]),
    ("export GITHUB_TOKEN=FAKEgh0123", ["FAKEgh0123"]),
    ('DB_PASSWORD="FAKE pass words"', ["FAKE pass words"]),
    ("AWS_SECRET_ACCESS_KEY = 'FAKEsecret/abc'", ["FAKEsecret/abc"]),
    ("client_secret=FAKEclient", ["FAKEclient"]),
    ("password=FAKEpw12", ["FAKEpw12"]),
    ("MY_CREDENTIAL=FAKEcred", ["FAKEcred"]),
    ("BASIC_AUTH=FAKEuser:FAKEpass", ["FAKEuser:FAKEpass"]),
    ('{"api_key": "FAKEjsonkey123"}', ["FAKEjsonkey123"]),
    ('{"refresh_token":"FAKErefresh"}', ["FAKErefresh"]),
    ("{'client-secret': 'FAKEsingle'}", ["FAKEsingle"]),
    ('"Password" : "FAKEcase"', ["FAKEcase"]),
    # kept: references, paths, lookups, lookalike names, prose
    ("OPENAI_API_KEY=${OPENAI_API_KEY}", []),
    ("OPENAI_API_KEY=$OPENAI_API_KEY", []),
    ("SSH_KEY_FILE=/home/u/.ssh/id_ed25519", []),
    ("KEY_PATH=~/keys/dev.pem", []),
    ("api_key = os.environ['OPENAI_API_KEY']", []),
    ('token = os.getenv("GH_TOKEN")', []),
    ("/usr/bin/key", []),
    ("MONKEY=banana", []),
    ("AUTHOR=someone", []),
    ("KEYBOARD=us", []),
    ("TOKENIZER=fast", []),
    ("api_key=None", []),
    ('{"token": null}', []),
    ("the key point is clarity", []),
    ("sk-short", []),
    ("https://example.com/path?page=2&sort=asc", []),
    ("https://example.com/user@host", []),
    ("eyJ.not.a.jwt", []),
]


def test_table_has_enough_rows() -> None:
    assert len(CASES) >= 40
    assert sum(1 for _, gone in CASES if not gone) >= 15


@pytest.mark.parametrize(("text", "gone"), CASES)
def test_table(text: str, gone: list[str]) -> None:
    out = redact_text(text)
    if not gone:
        assert out == text
        return
    assert out != text
    for secret in gone:
        assert secret not in out


def test_openai_assignment_masked_with_ends_kept() -> None:
    assert redact_text(f"OPENAI_API_KEY={FAKE_OPENAI}") == "OPENAI_API_KEY=sk-p…6789"


def test_url_masked_twice() -> None:
    out = redact_text("https://u:p@h/x?token=t")
    assert out == f"https://u:{MASK_SHORT}@h/x?token={MASK_SHORT}"
    assert out.count(MASK_SHORT) == 2


def test_overlapping_finds_masked_once() -> None:
    out = redact_text(f"Authorization: Bearer {FAKE_OPENAI}")
    assert out == "Authorization: Bearer sk-p…6789"


def test_mask_format() -> None:
    assert mask("abcdefghijkl") == "abcd…ijkl"
    assert mask("abcdefghijk") == MASK_SHORT
    assert mask("") == MASK_SHORT


def test_stored_secret_in_free_text(scope: FakeScope) -> None:
    scope.secrets.set("CUSTOM_THING", "plainlooking-value-42")
    out = Redactor(scope).redact(
        "the server said plainlooking-value-42 twice: plainlooking-value-42"
    )
    assert "plainlooking-value-42" not in out
    assert out.count("plai…e-42") == 2


def test_env_file_values_masked_but_public_ones_kept(scope: FakeScope) -> None:
    (scope.home / ".env").write_text(
        "WEIRD=opaque-value-777\nBASE_URL=https://api.example.com/v1\nPORT=8080\n"
        "WORKDIR=/srv/projects/app\nSHORT=abc\n"
    )
    out = Redactor(scope).redact(
        "opaque-value-777 at https://api.example.com/v1 port 8080 in /srv/projects/app abc"
    )
    assert "opaque-value-777" not in out
    assert "https://api.example.com/v1 port 8080 in /srv/projects/app abc" in out


def test_env_file_change_is_picked_up(scope: FakeScope) -> None:
    redactor = Redactor(scope)
    assert redactor.redact("value-one-1111") == "value-one-1111"
    (scope.home / ".env").write_text("X=value-one-1111\n")
    assert redactor.redact("value-one-1111") == "valu…" + "1111"


def test_store_change_is_picked_up(scope: FakeScope) -> None:
    redactor = Redactor(scope)
    assert redactor.redact("fresh-secret-9") == "fresh-secret-9"
    scope.secrets.set("LATE", "fresh-secret-9")
    assert redactor.redact("fresh-secret-9") == "fres…et-9"


def test_longest_value_first(scope: FakeScope) -> None:
    scope.secrets.set("SHORT_ONE", "abcdefgh")
    scope.secrets.set("LONG_ONE", "abcdefgh-and-more")
    out = Redactor(scope).redact("x abcdefgh-and-more y")
    assert out == "x abcd…more y"


def test_switch_off_in_config_leaves_text(scope: FakeScope) -> None:
    from indus.foundation.envload import build_env

    scope.env = dict(build_env(scope.home, {}, config={"security": {"redact_secrets": False}}))
    text = f"OPENAI_API_KEY={FAKE_OPENAI}"
    redactor = Redactor(scope)
    assert redactor.enabled is False
    assert redactor.redact(text) == text
    assert redactor.redact_obj({"k": text}) == {"k": text}
    assert FAKE_OPENAI not in redactor.redact(text, force=True)
    assert FAKE_OPENAI not in str(redactor.redact_obj({"k": text}, force=True))


def test_switch_read_per_call(scope: FakeScope) -> None:
    redactor = Redactor(scope)
    text = f"x {FAKE_GITHUB}"
    assert redactor.redact(text) != text
    scope.env = {"INDUS_REDACT_SECRETS": "false"}
    assert redactor.redact(text) == text
    scope.env = {}
    assert redactor.redact(text) != text


def test_redact_obj_nesting_and_keys(scope: FakeScope) -> None:
    scope.secrets.set("STORED", "stored-secret-1")
    data = {
        "api_key": "short",
        "password": "${DB_PASSWORD}",
        "note": "uses stored-secret-1",
        "list": [f"Bearer {FAKE_OPENAI}", 3, None, ("ok", "stored-secret-1")],
        "nested": {"deeper": {"client_secret": "FAKEclientvalue"}},
        "set": {"stored-secret-1"},
        5: "number key",
    }
    out = Redactor(scope).redact_obj(data)
    assert out["api_key"] == MASK_SHORT
    assert out["password"] == "${DB_PASSWORD}"
    assert "stored-secret-1" not in out["note"]
    assert FAKE_OPENAI not in out["list"][0]
    assert out["list"][1:3] == [3, None]
    assert isinstance(out["list"][3], tuple) and out["list"][3][0] == "ok"
    assert out["nested"]["deeper"]["client_secret"] == "FAKE…alue"
    assert out["set"] == {"stor…et-1"}
    assert out[5] == "number key"
    assert data["api_key"] == "short"  # input untouched


def test_redact_obj_cycles(scope: FakeScope) -> None:
    loop: dict[str, Any] = {"name": "loop"}
    loop["self"] = loop
    items: list[Any] = ["a"]
    items.append(items)
    out = Redactor(scope).redact_obj({"loop": loop, "items": items})
    assert out["loop"]["self"] == CYCLE_MARKER
    assert out["items"] == ["a", CYCLE_MARKER]


def test_redact_obj_shared_reference_is_not_a_cycle(scope: FakeScope) -> None:
    shared = {"v": 1}
    assert Redactor(scope).redact_obj([shared, shared]) == [{"v": 1}, {"v": 1}]


def test_redact_obj_depth_cap(scope: FakeScope) -> None:
    deep: Any = "bottom"
    for _ in range(MAX_DEPTH + 10):
        deep = [deep]
    out = Redactor(scope).redact_obj(deep)
    for _ in range(MAX_DEPTH):
        out = out[0]
    assert out == DEPTH_MARKER


def test_broken_store_never_breaks_redaction(tmp_path: Path) -> None:
    class Broken:
        generation = 0

        def known_values(self) -> list[str]:
            raise RuntimeError("keychain gone")

    scope = FakeScope(home=tmp_path, secrets=Broken())
    assert FAKE_GITHUB not in Redactor(scope).redact(f"t {FAKE_GITHUB}")


def test_no_import_time_switch_snapshot() -> None:
    names = [n for n in vars(redact_mod) if "ENABLED" in n.upper()]
    assert names == []


def test_property_output_never_contains_a_stored_value(scope: FakeScope) -> None:
    # Seeded property check (hypothesis is not in the dev group yet).
    rng = random.Random(1717)
    alphabet = string.ascii_letters + string.digits + "-_.:/=+ "
    redactor = Redactor(scope)
    stored: list[str] = []
    for i in range(12):
        value = "".join(rng.choice(alphabet) for _ in range(rng.randint(8, 30)))
        scope.secrets.set(f"PROP_{i}", value)
        stored.append(value)
    for _ in range(400):
        parts: list[str] = []
        for _ in range(rng.randint(1, 6)):
            pick = rng.random()
            if pick < 0.4:
                parts.append(rng.choice(stored))
            elif pick < 0.5:
                value = rng.choice(stored)
                parts.append(value[: rng.randint(0, len(value))])
            else:
                parts.append("".join(rng.choice(alphabet) for _ in range(rng.randint(0, 20))))
        text = "".join(parts)
        out = redactor.redact(text)
        for value in stored:
            if len(value.strip()) >= 8:
                assert value not in out, (text, value)
