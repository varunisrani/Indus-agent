"""The ``security_static`` guard (P25-01).

``fixtures/security/<rule>.pysrc`` holds seeded violations: every line ending in
``# expect: <rule>`` must be reported with that rule and no other line may be. ``clean.pysrc``
holds lookalikes that must stay quiet. The fixtures are data (``.pysrc``): they are copied into a
temporary ``src/`` tree as ``.py`` files, so no linter or type checker reads them.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tooling.checks import NAMES, security_static
from tooling.checks.common import DEFAULT_ROOT
from tooling.checks.tests.conftest import FIXTURES, make_ctx, write

SEEDS = FIXTURES / "security"
EXPECT = re.compile(r"#\s*expect:\s*(\w+)\s*$")
DATED = "2026-10-21: reviewed"


def expected(text: str) -> list[tuple[int, str]]:
    return [
        (number, match.group(1))
        for number, line in enumerate(text.splitlines(), start=1)
        if (match := EXPECT.search(line))
    ]


def allow_file(tmp_path: Path, allow: dict[str, dict[str, str]]) -> Path:
    path = tmp_path / "allow.json"
    path.write_text(json.dumps({"note": "test", "allow": allow}), encoding="utf-8")
    return path


def seeded_rules() -> list[str]:
    return sorted(p.stem for p in SEEDS.glob("*.pysrc") if p.stem != "clean")


def test_registered() -> None:
    assert "security_static" in NAMES


def test_every_rule_has_a_seed() -> None:
    seeded = {
        rule
        for name in seeded_rules()
        for _, rule in expected((SEEDS / f"{name}.pysrc").read_text())
    }
    assert seeded == set(security_static.RULES)


@pytest.mark.parametrize("name", seeded_rules())
def test_seeded_violations_are_reported_exactly(name: str) -> None:
    text = (SEEDS / f"{name}.pysrc").read_text(encoding="utf-8")
    hits = [(h.line, h.rule) for h in security_static.find_hits(text, src=True)]
    assert sorted(hits) == sorted(expected(text))


def test_lookalikes_stay_quiet() -> None:
    text = (SEEDS / "clean.pysrc").read_text(encoding="utf-8")
    assert security_static.find_hits(text) == []


@pytest.mark.parametrize("name", seeded_rules())
def test_a_seeded_tree_fails_with_the_rule_id(tmp_path: Path, name: str) -> None:
    root = tmp_path / "repo"
    write(root / "src" / "pkg" / f"{name}.py", (SEEDS / f"{name}.pysrc").read_text())
    result = security_static.run(make_ctx(root), allow_file(tmp_path, {}))
    assert result.code == 1
    rules = {rule for _, rule in expected((SEEDS / f"{name}.pysrc").read_text())}
    for rule in rules:
        assert any(f"[{rule}]" in v and f"src/pkg/{name}.py:" in v for v in result.violations)


def test_src_only_rules_skip_scripts(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    write(root / "scripts" / "tool.py", (SEEDS / "inherit_env.pysrc").read_text())
    write(root / "scripts" / "talk.py", (SEEDS / "secret_log.pysrc").read_text())
    write(root / "scripts" / "boom.py", (SEEDS / "shell.pysrc").read_text())
    result = security_static.run(make_ctx(root), allow_file(tmp_path, {}))
    assert result.code == 1
    assert not any("[inherit_env]" in v or "[secret_log]" in v for v in result.violations)
    assert any("scripts/boom.py" in v and "[shell]" in v for v in result.violations)


def test_allow_list_needs_a_dated_reason_and_reports_stale(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    write(root / "src" / "run.py", "import os\n\ndef go(c):\n    os.system(c)\n")
    write(root / "src" / "quiet.py", "x = 1\n")
    allow = allow_file(
        tmp_path,
        {
            "src/run.py": {"shell": DATED},
            "src/quiet.py": {"eval": DATED},
            "src/gone.py": {"shell": DATED},
        },
    )
    result = security_static.run(make_ctx(root), allow)
    assert result.code == 0
    assert result.data["allowed"] == ["src/run.py:shell"]
    assert len(result.warnings) == 2
    for bad in ("reviewed without a date", "", "2026-10-21:"):
        result = security_static.run(
            make_ctx(root), allow_file(tmp_path, {"src/run.py": {"shell": bad}})
        )
        assert result.code == 1 and any("YYYY-MM-DD" in v for v in result.violations)
    result = security_static.run(
        make_ctx(root), allow_file(tmp_path, {"src/run.py": {"nonsense": DATED}})
    )
    assert any("unknown rule" in v for v in result.violations)


def test_an_allowed_rule_does_not_cover_another_rule(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    write(root / "src" / "run.py", "import os\n\ndef go(c):\n    os.system(c)\n    eval(c)\n")
    result = security_static.run(
        make_ctx(root), allow_file(tmp_path, {"src/run.py": {"shell": DATED}})
    )
    assert result.code == 1
    assert [v.split(" — ")[0] for v in result.violations] == ["src/run.py:5: [eval] eval()"]


def test_unparsable_file_is_a_violation(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    write(root / "src" / "broken.py", "def (:\n")
    result = security_static.run(make_ctx(root), allow_file(tmp_path, {}))
    assert result.code == 1 and "cannot parse" in result.violations[0]


def test_secret_names_are_whole_words() -> None:
    secret = ["token", "bot_token", "API_KEY", "client_secret", "password", "ticket", "private_key"]
    plain = ["token_count", "max_tokens", "tokenizer", "keyring", "secret_name", "ticket_id"]
    assert all(security_static.is_secret_name(n) for n in secret)
    assert not any(security_static.is_secret_name(n) for n in plain)


def test_the_real_tree_passes() -> None:
    result = security_static.run(make_ctx(DEFAULT_ROOT))
    assert result.code == 0, result.violations
    assert not result.warnings, result.warnings
