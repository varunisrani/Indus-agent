"""Locale parity: keys, placeholders, value types, and the strict YAML subset reader."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tooling.checks import locales
from tooling.checks.common import DEFAULT_ROOT, Context, Result
from tooling.checks.tests.conftest import write

EN = """\
# English source
common:
  ok: OK
  files: "Found {count} files in {folder}"
errors:
  store:
    locked: The store at {path} is busy.
"""


def run(root: Path) -> Result:
    return locales.run(Context(root=root))


def repo(tmp_path: Path, **files: str) -> Path:
    write(tmp_path / "locales" / "en.yaml", EN)
    for name, text in files.items():
        write(tmp_path / "locales" / name.replace("_DOT_", "."), text)
    return tmp_path


FR = """\
common:
  ok: D'accord
  files: "{count} fichiers dans {folder}"
errors:
  store:
    locked: Le magasin {path} est occupé.
"""


def test_english_only_passes(tmp_path: Path):
    result = run(repo(tmp_path))
    assert result.code == 0, result.violations
    assert result.data["keys"] == 3


def test_matching_translation_passes(tmp_path: Path):
    assert run(repo(tmp_path, **{"fr_DOT_yaml": FR})).code == 0


def test_missing_key_fails(tmp_path: Path):
    text = FR.replace("  ok: D'accord\n", "")
    result = run(repo(tmp_path, **{"fr_DOT_yaml": text}))
    assert result.code == 1
    assert result.violations == [
        "locales/fr.yaml: missing key common.ok -> add it (English is the source)"
    ]


def test_extra_key_fails(tmp_path: Path):
    result = run(repo(tmp_path, **{"fr_DOT_yaml": FR + "cli:\n  bonus: Encore\n"}))
    assert result.code == 1 and "extra key cli.bonus" in result.violations[0]


def test_placeholder_mismatch_fails(tmp_path: Path):
    text = FR.replace("{count} fichiers", "{n} fichiers")
    result = run(repo(tmp_path, **{"fr_DOT_yaml": text}))
    assert result.code == 1
    assert "common.files" in result.violations[0] and "{count, folder}" in result.violations[0]


@pytest.mark.parametrize("value", ["42", "true", "yes", "null", "~", "3.5", "2026-01-01", ""])
def test_non_string_value_fails(tmp_path: Path, value: str):
    text = FR.replace("ok: D'accord", f"ok: {value}".rstrip())
    result = run(repo(tmp_path, **{"fr_DOT_yaml": text}))
    assert result.code == 1 and "is not a string" in result.violations[0]


def test_quoted_number_is_a_string(tmp_path: Path):
    text = FR.replace("ok: D'accord", 'ok: "42"')
    assert run(repo(tmp_path, **{"fr_DOT_yaml": text})).code == 0


def test_duplicate_key_fails(tmp_path: Path):
    text = FR.replace("  ok: D'accord\n", "  ok: D'accord\n  ok: Oui\n")
    result = run(repo(tmp_path, **{"fr_DOT_yaml": text}))
    assert result.code == 1 and "duplicate key 'ok'" in result.violations[0]


def test_dotted_key_colliding_with_nested_fails(tmp_path: Path):
    text = FR + '"errors.store.locked": Encore\n'
    result = run(repo(tmp_path, **{"fr_DOT_yaml": text}))
    assert result.code == 1 and "defined twice" in result.violations[0]


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("ok: &a D'accord", "anchors"),
        ("ok: *a", "aliases"),
        ("ok: !str D'accord", "tags"),
        ("ok: [a, b]", "flow sequences"),
        ("ok: {count} fichiers", "flow mappings"),
        ("ok: |", "block scalars"),
        ("ok: >", "folded scalars"),
        ("ok: a: b", "may not contain ': '"),
        ('ok: "open', "unterminated quote"),
        ('ok: "a" b', "after the closing quote"),
        ('ok: "bad \\q"', "unknown escape"),
    ],
)
def test_unsupported_syntax_is_refused_with_a_message(tmp_path: Path, line: str, message: str):
    text = FR.replace("ok: D'accord", line)
    result = run(repo(tmp_path, **{"fr_DOT_yaml": text}))
    assert result.code == 1
    assert result.violations[0].startswith("locales/fr.yaml:2: "), result.violations
    assert message in result.violations[0]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("- a\n", "sequences"),
        ("---\na: b\n", "document markers"),
        ("a:\n\tb: c\n", "tabs"),
        ("a:\n    b: c\n  d: e\n", "indentation"),
        ("  a: b\n", "indentation"),
        ("? a\n", "complex keys"),
        ("a b\n", "expected ':'"),
    ],
)
def test_structure_errors(text: str, message: str):
    with pytest.raises(locales.LocaleSyntaxError, match=message):
        locales.parse(text)


def test_reader_values():
    tree = locales.flatten(
        locales.parse(
            "\ufeff# top comment\n"
            "a:\n"
            "  b: plain text # trailing comment\n"
            "  c: 'it''s #1'\n"
            '  d: "tab\\there \\u00e9 \\"q\\"" # note\n'
            "\n"
            "  # indented comment\n"
            "  e:\n"
            "    f: url http://x/y#frag\n"
            '"g.h": quoted key\n'
        )
    )
    assert {k: v.text for k, v in tree.items()} == {
        "a.b": "plain text",
        "a.c": "it's #1",
        "a.d": 'tab\there é "q"',
        "a.e.f": "url http://x/y#frag",
        "g.h": "quoted key",
    }


def test_placeholders():
    assert locales.placeholders("{a} {b!r} {c:>4} {{literal}} {0}") == {"a", "b", "c"}


def test_missing_english_fails(tmp_path: Path):
    write(tmp_path / "locales" / "fr.yaml", FR)
    result = run(tmp_path)
    assert result.code == 1 and "en.yaml: missing" in result.violations[0]


def test_no_locales_folder_passes(tmp_path: Path):
    assert run(tmp_path).code == 0


def test_yml_extension_fails(tmp_path: Path):
    assert run(repo(tmp_path, **{"fr_DOT_yml": FR})).code == 1


def test_surface_pack_against_key_list(tmp_path: Path):
    keys = json.dumps(["status.busy", "status.idle"])
    ok = "status:\n  busy: Occupé\n  idle: Libre\n"
    root = repo(tmp_path, **{"_keys_DOT_tui_DOT_json": keys, "fr_DOT_tui_DOT_yaml": ok})
    assert run(root).code == 0
    write(root / "locales" / "de.tui.yaml", "status:\n  busy: Beschäftigt\n  extra: Mehr\n")
    text = "\n".join(run(root).violations)
    assert "de.tui.yaml: missing key status.idle" in text
    assert "extra key status.extra" in text


def test_surface_key_list_with_english_checks_placeholders(tmp_path: Path):
    keys = json.dumps({"turns": "{n} turns"})
    root = repo(tmp_path, **{"_keys_DOT_tui_DOT_json": keys, "fr_DOT_tui_DOT_yaml": "turns: x\n"})
    result = run(root)
    assert result.code == 1 and "placeholders" in result.violations[0]


def test_surface_pack_without_key_list_fails(tmp_path: Path):
    result = run(repo(tmp_path, **{"fr_DOT_desktop_DOT_yaml": "a: b\n"}))
    assert result.code == 1 and "_keys.desktop.json" in result.violations[0]


def test_real_repo_passes():
    result = locales.run(Context(root=DEFAULT_ROOT))
    assert result.code == 0, result.violations
    assert "en" in result.data["languages"]
