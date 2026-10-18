"""The YAML seam (P01-08) and its import ratchet (R04.5)."""

from __future__ import annotations

import ast
import shutil
from pathlib import Path

import pytest

from indus.foundation import yamlio

REPO = Path(__file__).resolve().parents[3]
SRC = REPO / "src"
FIXTURES = REPO / "tests" / "fixtures" / "config"
SEAM = Path("indus") / "foundation" / "yamlio.py"
BANNED_ROOTS = ("ruamel", "yaml")


def find_yaml_imports(src: Path) -> list[str]:
    """Every module under ``src`` (except the seam) that imports ruamel or yaml."""
    offenders: list[str] = []
    for file in sorted(src.rglob("*.py")):
        rel = file.relative_to(src)
        if rel == SEAM:
            continue
        tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
        for node in ast.walk(tree):
            names: list[tuple[int, str]] = []
            if isinstance(node, ast.Import):
                names = [(node.lineno, alias.name) for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [(node.lineno, node.module)]
            for lineno, name in names:
                if name.split(".")[0] in BANNED_ROOTS:
                    offenders.append(f"{rel}:{lineno}: {name}")
    return offenders


def test_yaml_import_ratchet_on_src() -> None:
    assert find_yaml_imports(SRC) == []


def test_yaml_import_ratchet_catches_planted_import(tmp_path: Path) -> None:
    copy = tmp_path / "src"
    shutil.copytree(SRC, copy, ignore=shutil.ignore_patterns("__pycache__"))
    planted = copy / "indus" / "core" / "sneaky.py"
    planted.write_text("from ruamel.yaml import YAML\nimport yaml\n", encoding="utf-8")
    found = find_yaml_imports(copy)
    assert found == [
        "indus/core/sneaky.py:1: ruamel.yaml",
        "indus/core/sneaky.py:2: yaml",
    ]


def test_yaml11_booleans() -> None:
    # Documented YAML 1.1 behaviour: bare on/off/yes/no are booleans, keys included.
    assert yamlio.load_safe("on: yes\nmode: off\nn: y\nquoted: 'off'\n") == {
        True: True,
        "mode": False,
        False: True,
        "quoted": "off",
    }


def test_duplicate_key_names_line() -> None:
    with pytest.raises(yamlio.YamlError) as info:
        yamlio.load_safe("approvals:\n  mode: manual\nlogging: {}\napprovals:\n  mode: off\n")
    assert info.value.line == 4
    assert "line 4" in str(info.value)


def test_parse_error_carries_line() -> None:
    with pytest.raises(yamlio.YamlError) as info:
        yamlio.load_safe((FIXTURES / "broken.yaml").read_bytes())
    assert info.value.line is not None and 4 <= info.value.line <= 5


def test_bom_tolerated(tmp_path: Path) -> None:
    path = tmp_path / "bom.yaml"
    path.write_bytes(b"\xef\xbb\xbfa: 1\n")
    assert yamlio.load_safe(path) == {"a": 1}
    assert yamlio.load_safe("\ufeffa: 1\n") == {"a": 1}
    assert dict(yamlio.load_roundtrip(path)) == {"a": 1}


def test_invalid_utf8_is_yaml_error() -> None:
    with pytest.raises(yamlio.YamlError, match="UTF-8"):
        yamlio.load_safe(b"a: \xff\n")


def test_empty_and_missing() -> None:
    assert yamlio.load_safe("") is None
    assert dict(yamlio.load_roundtrip("")) == {}
    assert dict(yamlio.load_roundtrip(Path("/nonexistent/indus/x.yaml"))) == {}


def test_roundtrip_root_must_be_mapping() -> None:
    with pytest.raises(yamlio.YamlError, match="mapping"):
        yamlio.load_roundtrip("- a\n- b\n")


def test_roundtrip_is_byte_identical() -> None:
    text = (FIXTURES / "commented.yaml").read_text(encoding="utf-8")
    assert yamlio.dump_roundtrip(yamlio.load_roundtrip(text)) == text


def test_editing_one_value_keeps_every_comment() -> None:
    text = (FIXTURES / "commented.yaml").read_text(encoding="utf-8")
    doc = yamlio.load_roundtrip(text)
    doc["logging"]["level"] = "ERROR"
    out = yamlio.dump_roundtrip(doc)
    assert out == text.replace("level: WARNING", "level: ERROR")


def test_long_double_quoted_value_is_not_folded() -> None:
    value = "C:\\\\Data\\\\" + "segment " * 40
    text = f'path: "{value}"\n'
    assert yamlio.dump_roundtrip(yamlio.load_roundtrip(text)) == text


def test_new_ambiguous_strings_are_quoted() -> None:
    doc = yamlio.load_roundtrip("a: 1\n")
    for key, value in {"m": "off", "t": "12:30", "o": "017", "y": "yes", "plain": "hello"}.items():
        doc[yamlio.roundtrip_key(key)] = yamlio.to_roundtrip(value)
    doc["nested"] = yamlio.to_roundtrip({"list": ["on", "x"], "no": "NO"})
    doc[yamlio.roundtrip_key("on")] = 1
    out = yamlio.dump_roundtrip(doc)
    assert "plain: hello" in out
    reread = yamlio.load_safe(out)
    assert reread["m"] == "off" and reread["t"] == "12:30" and reread["o"] == "017"
    assert reread["y"] == "yes" and reread["on"] == 1
    assert reread["nested"] == {"list": ["on", "x"], "no": "NO"}


def test_is_ambiguous_string() -> None:
    assert yamlio.is_ambiguous_string("off")
    assert yamlio.is_ambiguous_string("~")
    assert yamlio.is_ambiguous_string("1.5")
    assert not yamlio.is_ambiguous_string("manual")
    assert not yamlio.is_ambiguous_string("0o17")


def test_dump_plain() -> None:
    data = {"b": "off", "a": [1, 2], "c": {}, "u": "héllo", "t": "12:30"}
    out = yamlio.dump_plain(data)
    assert not out.startswith("%YAML")
    assert out.index("b:") < out.index("a:")
    assert "  - 1\n" in out
    assert "héllo" in out
    assert yamlio.load_safe(out) == data
