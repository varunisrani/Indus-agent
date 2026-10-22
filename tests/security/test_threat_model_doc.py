"""The threat model and this suite stay in step (P25-01 / P25-02).

- every test path the document cites exists, and a cited ``::name`` is defined in that file;
- every test of this suite is cited by the document, and its row id (docstring) is a row there;
- every row has a proof or says who owns it; every finding has a severity and a decision.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "internals" / "threat-model.md"
SUITE = Path(__file__).resolve().parent
CITED = re.compile(r"`((?:tests|tooling|apps|scripts)/[^`\s:]+)(?:::([A-Za-z_][\w]*))?`")
ROW = re.compile(r"^\| (B-[A-Z]+-\d+) \|", re.MULTILINE)
FINDING = re.compile(r"^\| (SEC-\d+) \| (critical|high|medium|low|info) \|", re.MULTILINE)
ROW_ID = re.compile(r"\bB-[A-Z]+-\d+\b")
SEVERITY = re.compile(r"\| SEC-\d+ \| (\w+) \|")


def doc_text() -> str:
    return DOC.read_text(encoding="utf-8")


def suite_tests() -> dict[str, tuple[str, str]]:
    """``file::name`` → (file name, docstring) for every test function of the suite."""
    found: dict[str, tuple[str, str]] = {}
    for path in sorted(SUITE.glob("test_*.py")):
        if path.name == Path(__file__).name:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
                key = f"tests/security/{path.name}::{node.name}"
                found[key] = (path.name, ast.get_docstring(node) or "")
    return found


def test_every_cited_path_exists() -> None:
    missing: list[str] = []
    for match in CITED.finditer(doc_text()):
        rel, name = match.group(1), match.group(2)
        path = ROOT / rel
        if rel.endswith("/") and path.is_dir():
            continue
        if not path.is_file():
            missing.append(rel)
            continue
        if name and not re.search(rf"^\s*(?:async\s+)?def {name}\b", path.read_text(), re.M):
            missing.append(f"{rel}::{name}")
    assert not missing, missing


def test_every_suite_test_is_cited_with_a_known_row() -> None:
    text = doc_text()
    rows = set(ROW.findall(text))
    for key, (_file, docstring) in suite_tests().items():
        assert f"`{key}`" in text, f"{key} is not cited in {DOC.name}"
        ids = ROW_ID.findall(docstring)
        assert ids, f"{key} names no row id in its docstring"
        assert set(ids) <= rows, f"{key}: {set(ids) - rows} are not rows of {DOC.name}"


def test_rows_are_unique_and_have_proofs() -> None:
    text = doc_text()
    rows = ROW.findall(text)
    assert len(rows) == len(set(rows)), "duplicate row ids"
    assert len(rows) >= 50
    for line in text.splitlines():
        if ROW.match(line):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            proof, status = cells[3], cells[4]
            assert proof and status, line
            assert "`" in proof or "P25-0" in proof, f"no proof: {line[:60]}"


def test_findings_have_severity_and_decision() -> None:
    text = doc_text()
    ids = [m.group(1) for m in FINDING.finditer(text)]
    assert ids and len(ids) == len(set(ids))
    assert [int(i.split("-")[1]) for i in ids] == list(range(1, len(ids) + 1))
    assert len(SEVERITY.findall(text)) == len(ids)  # no row with an unknown severity
    for line in text.splitlines():
        if FINDING.match(line):
            decision = line.split("|")[4].lower()
            assert any(w in decision for w in ("fixed", "open", "accepted", "allow-listed",
                                               "owner decision")), line[:60]  # fmt: skip
