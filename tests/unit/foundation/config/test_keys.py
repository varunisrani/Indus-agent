"""Dotted keys and value coercion (P01-13): R04.7, R04.8."""

from __future__ import annotations

import random
import shutil
from pathlib import Path
from typing import Any

import pytest

from indus.foundation import yamlio
from indus.foundation.config import keys
from indus.foundation.config.keys import CoercionError, GuardError, KeyPathError

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "config"


def _dotted() -> dict[str, Any]:
    return yamlio.load_safe(FIXTURES / "dotted_models.yaml")


# -- split / join -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "parts"),
    [
        ("a", ["a"]),
        ("a.b.c", ["a", "b", "c"]),
        (r"providers.p.models.grok-4\.6", ["providers", "p", "models", "grok-4.6"]),
        (r"a\\.b", ["a\\", "b"]),
        (r"a\\\.b", ["a\\.b"]),
        (r"win\path.x", ["win\\path", "x"]),
    ],
)
def test_split_key_escapes(key: str, parts: list[str]) -> None:
    assert keys.split_key(key) == parts


@pytest.mark.parametrize("bad", ["", " ", " a", "a ", "a..b", "a.", ".a", "."])
def test_split_key_refuses_empty(bad: str) -> None:
    with pytest.raises(KeyPathError):
        keys.split_key(bad)


def test_join_key_examples() -> None:
    assert keys.join_key(["providers", "grok-4.6"]) == r"providers.grok-4\.6"
    assert keys.join_key(["a\\"]) == "a\\\\"
    with pytest.raises(KeyPathError):
        keys.join_key([])
    with pytest.raises(KeyPathError):
        keys.join_key(["a", ""])


def _roundtrip(parts: list[str]) -> None:
    assert keys.split_key(keys.join_key(parts)) == parts


def test_split_join_roundtrip_seeded() -> None:
    rng = random.Random(1604)
    alphabet = "ab.\\-_9 é"
    for _ in range(2000):
        parts = [
            "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 6)))
            for _ in range(rng.randint(1, 4))
        ]
        if any(p != p.strip() for p in parts[:1] + parts[-1:]):
            continue  # a key may not start or end with whitespace
        _roundtrip(parts)


def test_split_join_roundtrip_hypothesis() -> None:
    hypothesis = pytest.importorskip("hypothesis")
    st = pytest.importorskip("hypothesis.strategies")
    segment = st.text(alphabet=st.characters(blacklist_categories=("Cs",)), min_size=1).filter(
        lambda s: s == s.strip()
    )

    @hypothesis.given(st.lists(segment, min_size=1, max_size=5))
    def prop(parts: list[str]) -> None:
        _roundtrip(parts)

    prop()


def test_is_env_shaped() -> None:
    assert keys.is_env_shaped("OPENAI_API_KEY")
    assert keys.is_env_shaped("PATH")
    assert not keys.is_env_shaped("approvals.mode")
    assert not keys.is_env_shaped("Openai_Key")
    assert not keys.is_env_shaped("_X")


# -- navigation ---------------------------------------------------------------------------------


def test_get_prefers_literal_dotted_key() -> None:
    doc = _dotted()
    assert keys.get_path(doc, "providers.p.models.grok-4.6.context_length") == 131072
    assert keys.get_path(doc, r"providers.p.models.grok-4\.6.context_length") == 131072
    assert keys.get_path(doc, "providers.p.models.glm-5.3") == {"supports_vision": False}
    assert keys.resolve_path(doc, "providers.p.models.grok-4.6.context_length") == (
        "providers",
        "p",
        "models",
        "grok-4.6",
        "context_length",
    )


def test_longest_literal_wins() -> None:
    doc = {"a.b": {"c": 1}, "a": {"b": {"c": 2}}}
    assert keys.get_path(doc, "a.b.c") == 1


def test_get_missing_and_default() -> None:
    doc = _dotted()
    with pytest.raises(KeyError):
        keys.get_path(doc, "providers.p.models.grok-5")
    assert keys.get_path(doc, "providers.q", "fallback") == "fallback"
    assert not keys.has_path(doc, "providers.p.base_url.deeper")


def test_lists_are_indexed() -> None:
    doc: dict[str, Any] = {"approvals": {"deny": ["rm", "dd"]}}
    assert keys.get_path(doc, "approvals.deny.1") == "dd"
    assert keys.get_path(doc, "approvals.deny.-1") == "dd"
    assert not keys.has_path(doc, "approvals.deny.5")
    keys.set_path(doc, "approvals.deny.0", "mkfs")
    assert doc["approvals"]["deny"] == ["mkfs", "dd"]
    with pytest.raises(KeyPathError):
        keys.set_path(doc, "approvals.deny.2", "x")  # lists are never grown
    with pytest.raises(KeyPathError):
        keys.set_path(doc, "approvals.deny.first", "x")


def test_set_writes_into_literal_dotted_key() -> None:
    doc = _dotted()
    keys.set_path(doc, "providers.p.models.grok-4.6.supports_vision", True)
    assert doc["providers"]["p"]["models"]["grok-4.6"] == {
        "context_length": 131072,
        "supports_vision": True,
    }
    assert "grok-4" not in doc["providers"]["p"]["models"]


def test_set_refuses_phantom_sibling() -> None:
    doc = _dotted()
    with pytest.raises(KeyPathError, match=r"grok-4\\\\.6"):
        keys.set_path(doc, "providers.p.models.grok-4.7.context_length", 1)
    assert set(doc["providers"]["p"]["models"]) == {"grok-4.6", "glm-5.3"}


def test_set_with_escape_creates_dotted_key() -> None:
    doc = _dotted()
    keys.set_path(doc, r"providers.p.models.grok-4\.7.context_length", 1)
    assert doc["providers"]["p"]["models"]["grok-4.7"] == {"context_length": 1}


def test_set_creates_sections_and_refuses_scalar_parent() -> None:
    doc: dict[str, Any] = {"timezone": "UTC"}
    keys.set_path(doc, "approvals.mode", "off")
    assert doc["approvals"] == {"mode": "off"}
    with pytest.raises(KeyPathError, match="holds a value"):
        keys.set_path(doc, "timezone.name", "x")
    assert doc["timezone"] == "UTC"


def test_unset_drops_emptied_sections() -> None:
    doc: dict[str, Any] = {"a": {"b": {"c": 1}}, "keep": {"empty": []}}
    assert keys.unset_path(doc, "a.b.c")
    assert doc == {"keep": {"empty": []}}
    assert not keys.unset_path(doc, "a.b.c")
    dotted = _dotted()
    assert keys.unset_path(dotted, "providers.p.models.grok-4.6")
    assert set(dotted["providers"]["p"]["models"]) == {"glm-5.3"}


def test_fixture_file_untouched_by_reads(tmp_path: Path) -> None:
    copy = tmp_path / "d.yaml"
    shutil.copy(FIXTURES / "dotted_models.yaml", copy)
    keys.get_path(yamlio.load_safe(copy), "providers.p.models.grok-4.6")
    assert copy.read_bytes() == (FIXTURES / "dotted_models.yaml").read_bytes()


# -- coercion (R04.7) ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "slot", "expected"),
    [
        ("off", str, "off"),  # string slots keep the literal
        ("yes", str, "yes"),
        ("[a, b]", str, "[a, b]"),
        ("  padded ", str, "  padded "),
        ("off", bool, False),
        ("On", bool, True),
        ("no", None, False),
        ("null", int, None),
        ("~", None, None),
        ("42", int, 42),
        ("-5", int, -5),
        ("1_000", int, 1000),
        ("2.5", float, 2.5),
        ("0.10000000000000000001", float, "0.10000000000000000001"),
        ("nan", float, "nan"),
        ("inf", None, "inf"),
        ("[rm, mkfs]", list, ["rm", "mkfs"]),
        ('{"a": 1}', dict, {"a": 1}),
        ("- rm\n- dd\n", list, ["rm", "dd"]),
        ("base_url: http://h\n", dict, {"base_url": "http://h"}),
        ("'[text'", list, "[text"),
        ('"42"', int, "42"),
        ("'off'", bool, "off"),
        ("hello world", None, "hello world"),
        ("--flag", None, "--flag"),
    ],
)
def test_coerce_table(raw: str, slot: type | None, expected: Any) -> None:
    value = keys.coerce(raw, slot)
    assert value == expected
    assert type(value) is type(expected)


@pytest.mark.parametrize("raw", ["[a, b", "{a: [1}", "- a\n  - : b\n:"])
def test_coerce_refuses_malformed_structured(raw: str) -> None:
    with pytest.raises(CoercionError, match="nothing was written"):
        keys.coerce(raw, list, key="approvals.deny")


def test_bare_model_structured_literal_is_parsed() -> None:
    assert keys.coerce("{provider: x, default: test-model}", str, key="model") == {
        "provider": "x",
        "default": "test-model",
    }
    assert keys.coerce("test-model", str, key="model") == "test-model"


# -- guards -------------------------------------------------------------------------------------


def test_check_write_section_guard() -> None:
    doc: dict[str, Any] = {"approvals": {"mode": "manual", "deny": ["rm"]}}
    with pytest.raises(GuardError, match="section"):
        keys.check_write(doc, "approvals", "off")
    assert keys.check_write(doc, "approvals", "off", force=True) == ("approvals", "off")
    assert keys.check_write(doc, "timezone", "UTC") == ("timezone", "UTC")


def test_check_write_model_shorthand() -> None:
    doc: dict[str, Any] = {"model": {"default": "a", "provider": "p"}}
    assert keys.check_write(doc, "model", "b") == ("model.default", "b")
    with pytest.raises(GuardError):
        keys.check_write(doc, "model", {"default": "b"})
    with pytest.raises(GuardError):
        keys.check_write({}, "model", ["a"])
    assert keys.check_write(doc, "model", {"default": "b"}, force=True) == (
        "model",
        {"default": "b"},
    )
    assert keys.check_write({"model": "a"}, "model", "b") == ("model", "b")


def test_check_write_container_guard() -> None:
    doc: dict[str, Any] = {"custom": {"items": [1, 2]}}
    with pytest.raises(GuardError, match="list"):
        keys.check_write({}, "approvals.deny", "rm")
    with pytest.raises(GuardError, match="list"):
        keys.check_write({}, "approvals.deny", "rm", force=True)  # never readable
    assert keys.check_write({}, "approvals.deny", ["rm"]) == ("approvals.deny", ["rm"])
    with pytest.raises(GuardError, match="mapping"):
        keys.check_write({}, "providers", "x")
    assert keys.check_write({}, "providers", "x", force=True) == ("providers", "x")
    with pytest.raises(GuardError, match="mapping"):
        keys.check_write({}, "database.pragmas", "x")
    with pytest.raises(GuardError, match="list"):
        keys.check_write(doc, "custom.items", "3")  # shape already on disk
    assert keys.check_write({}, "approvals.mode", "off") == ("approvals.mode", "off")
