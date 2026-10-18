"""Banned identifiers. Banned words are assembled at runtime so this file stays clean."""

from __future__ import annotations

import json
from pathlib import Path

from tooling.checks import banned
from tooling.checks.common import Context
from tooling.checks.tests.conftest import write

H = "her" + "mes"
N = "no" + "us"


def scan(text: str) -> list[tuple[int, int, str, str]]:
    patterns, problems = banned.load_patterns()
    assert not problems
    return banned.scan_text(text, patterns)


def test_seed_patterns_load_and_have_hints():
    patterns, problems = banned.load_patterns()
    assert not problems and len(patterns) >= 5
    assert all(p.hint for p in patterns)


def test_catches_every_seed_form():
    samples = [
        H,
        H.capitalize(),
        H.upper() + "_HOME",
        H + "_cli",
        H + "-agent",
        "~/." + H + "/config.yaml",
        "path/." + H + "/x",
        N.capitalize() + "Research",
        N + "research",
        "by " + N.capitalize() + " team",
    ]
    for s in samples:
        assert scan(f"x = '{s}'\n"), s


def test_specific_hint_wins_at_same_position():
    found = scan(H.upper() + "_HOME=1\n")
    assert len(found) == 1
    assert "INDUS_" in found[0][3]


def test_clean_words_pass():
    assert scan("nousle anonymous thermostat; Indus Agent; ~/.indus; INDUS_HOME\n") == []


def test_reports_line_and_column(tmp_path: Path):
    write(tmp_path / "src" / "indus" / "a.py", "ok = 1\nname = '" + H + "'\n")
    result = banned.run(Context(root=tmp_path))
    assert result.code == 1
    assert result.violations[0].startswith("src/indus/a.py:2:9:")


def test_paths_scanned_and_exempt_places(tmp_path: Path):
    write(tmp_path / "scripts" / (H + "_tool.sh"), "echo hi\n")
    write(tmp_path / "docs" / "x.md", H + "\n")
    write(tmp_path / "README.md", H + "\n")
    write(tmp_path / "tooling" / "checks" / "tests" / "t.py", H + "\n")
    write(tmp_path / "tooling" / "checks" / "banned_patterns.json", H + "\n")
    write(tmp_path / "apps" / "web" / "logo.png", "\0binary " + H)
    result = banned.run(Context(root=tmp_path))
    assert len(result.violations) == 1
    assert "path contains" in result.violations[0]


def test_bad_pattern_file_is_setup_problem(tmp_path: Path):
    bad = write(tmp_path / "p.json", json.dumps([{"pattern": "(", "hint": "x"}]))
    assert banned.run(Context(root=tmp_path), bad).code == 2
    nohint = write(tmp_path / "q.json", json.dumps([{"pattern": "x"}]))
    assert banned.run(Context(root=tmp_path), nohint).code == 2


def test_lead_can_append_patterns(tmp_path: Path):
    extra = json.loads(banned.PATTERNS_FILE.read_text(encoding="utf-8"))
    extra.append({"pattern": r"\bzorblax\b", "hint": "use widget", "flags": ["IGNORECASE"]})
    path = write(tmp_path / "p.json", json.dumps(extra))
    write(tmp_path / "src" / "a.py", "ZORBLAX = 1\n")
    result = banned.run(Context(root=tmp_path), path)
    assert result.code == 1 and "use widget" in result.violations[0]


def test_repository_itself_is_clean():
    from tooling.checks.common import DEFAULT_ROOT

    assert banned.run(Context(root=DEFAULT_ROOT)).violations == []
