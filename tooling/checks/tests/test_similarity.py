"""Similarity: unit extraction, gate, allow-list, closest mode, real-reference detection."""

from __future__ import annotations

import ast
import io
import json
import tokenize
from pathlib import Path

import pytest

from tooling.checks import similarity, simtext
from tooling.checks.common import Context
from tooling.checks.tests.conftest import write

# Synthetic "reference" content, invented for these tests.
REF_PY = '''\
import os


def blend_palette(colors, weight=0.5, clamp=True):
    """Mix every colour in the palette towards its neighbour by weight."""
    mixed = []
    for index, (red, green, blue) in enumerate(colors):
        nxt = colors[(index + 1) % len(colors)]
        r = red * (1 - weight) + nxt[0] * weight
        g = green * (1 - weight) + nxt[1] * weight
        b = blue * (1 - weight) + nxt[2] * weight
        if clamp:
            r, g, b = (max(0, min(255, int(v))) for v in (r, g, b))
        mixed.append((r, g, b))
    return mixed
'''

REF_MD = """# Palette notes

The lantern keeper counts seventeen copper bells before the harbour fog lifts each morning.
"""

REF_TS = """
export function lanternCount(bells: number): number {
  const fog = bells * 2;
  const harbour = fog + 3;
  const copper = harbour - bells;
  const keeper = copper * fog;
  const morning = keeper / 2;
  const lift = morning + harbour;
  return lift;
}
"""


@pytest.fixture
def tiny_ref(tmp_path: Path) -> Path:
    ref = tmp_path / "ref"
    write(ref / "lib" / "palette.py", REF_PY)
    write(ref / "docs" / "notes.md", REF_MD)
    write(ref / "web" / "lantern.ts", REF_TS)
    write(ref / "node_modules" / "skip" / "palette.py", REF_PY)
    write(ref / "package-lock.json", "{}")
    return ref


def run_sim(root: Path, ref: Path, **kw) -> similarity.Result:
    return similarity.run(Context(root=root, ref=ref, **kw))


# --------------------------------------------------------------------------- units


def test_python_tokens_drop_comments_and_layout():
    toks, _ = simtext.extract("a.py", "x = 1  # note\n\nif x:\n    y = 2\n")["tokens"]
    assert toks == ["x", "=", "1", "if", "x", ":", "y", "=", "2"]


def test_python_prose_has_comments_and_docstrings_only():
    src = 'def f():\n    """Spin The Wheel."""\n    s = "not prose"\n    return s  # Turn it\n'
    words, lines = simtext.extract("a.py", src)["prose"]
    assert words == ["spin", "the", "wheel", "turn", "it"]
    assert lines == [2, 2, 2, 4, 4]


def test_clike_scanner_strips_comments_but_not_urls_in_strings():
    src = 'const u = "http://x"; // trailing words\n/* block\nwords */ let z = 1;\n'
    units = simtext.extract("a.ts", src)
    assert units["tokens"][0] == ["const", "u", "=", '"http://x"', ";", "let", "z", "=", "1", ";"]
    assert units["prose"][0] == ["trailing", "words", "block", "words"]
    assert units["prose"][1] == [1, 1, 2, 3]


def test_meaningful_lines_drop_imports_brackets_comments():
    src = (
        "from a import (\n    b,\n    c,\n)\nimport os\n\n# comment\nx = 1\n"
        "def f():\n    return [\n        1,\n    ]\n"
    )
    lines, nums = simtext.meaningful_lines(src, "py")
    assert lines == ["x = 1", "def f():", "return [", "1,"]
    assert nums == [8, 9, 10, 11]


def test_meaningful_lines_skip_multiline_js_imports():
    src = "import {\n  a,\n  b,\n} from './x';\nconst q = require('y');\nlet k = a + b;\n"
    assert simtext.meaningful_lines(src, "clike")[0] == ["let k = a + b;"]


def test_unknown_extension_is_not_compared():
    assert simtext.extract("x.bin", "anything") == {}
    assert simtext.kind_of("Dockerfile") == "hash"


def test_identifier_list_lines_skipped_only_in_plan_docs(tmp_path: Path):
    line = "core/run_loop.py agent/state_db.py cli/main.py tools/x_y.py and"
    write(tmp_path / "docs" / "plan" / "a.md", line + "\n")
    write(tmp_path / "docs" / "notes" / "a.md", line + "\n")
    assert similarity.file_units(tmp_path, "docs/plan/a.md", product=True)["prose"][0] == []
    assert similarity.file_units(tmp_path, "docs/notes/a.md", product=True)["prose"][0]


def test_window_hashes_are_rolling_and_stable():
    units = [str(i) for i in range(10)]
    full = similarity.window_hashes(units, 4)
    assert len(full) == 7
    assert full[3] == similarity.window_hashes(units[3:7], 4)[0]
    assert similarity.window_hashes(units[:3], 4) == []


# --------------------------------------------------------------------------- gate


def test_reformatted_copy_with_new_comments_is_caught(tmp_path: Path, tiny_ref: Path, tiny_cache):
    body = REF_PY.split("\n\n\n", 1)[1]
    disguised = (
        body.replace("    ", "  ")
        .replace('"""Mix', "# our words\n  '''Mix")
        .replace('weight."""', "weight.'''")
    )
    root = tmp_path / "repo"
    write(root / "src" / "indus" / "mix.py", disguised)
    result = run_sim(root, tiny_ref)
    assert result.code == 1
    detectors = {h["detector"] for h in result.data["hits"]}
    assert "tokens" in detectors
    assert any("src/indus/mix.py:" in v and "lib/palette.py:" in v for v in result.violations)


def test_lines_detector_catches_six_lines(tmp_path: Path, tiny_ref: Path, tiny_cache):
    lines = [ln.strip() for ln in REF_TS.splitlines()[2:8]]
    root = tmp_path / "repo"
    write(root / "apps" / "web" / "x.ts", "function other() {\n" + "\n".join(lines) + "\n}\n")
    result = run_sim(root, tiny_ref)
    assert any(h["detector"] == "lines" and h["run"] >= 6 for h in result.data["hits"])


def test_prose_detector_needs_twelve_words(tmp_path: Path, tiny_ref: Path, tiny_cache):
    words = REF_MD.splitlines()[2].lower().replace(".", "").split()
    root = tmp_path / "repo"
    write(root / "docs" / "a.md", "We think " + " ".join(words[:12]) + " indeed.\n")
    write(root / "docs" / "b.md", "We think " + " ".join(words[:11]) + " indeed.\n")
    result = run_sim(root, tiny_ref)
    flagged = {v.split(":")[0] for v in result.violations}
    assert flagged == {"docs/a.md"}


def test_skipped_reference_folders_and_lockfiles(tiny_ref: Path):
    files = similarity.ref_files(tiny_ref)
    assert "lib/palette.py" in files
    assert not any(f.startswith("node_modules/") for f in files)
    assert "package-lock.json" not in files


def test_clean_synthetic_tree_passes(tmp_path: Path, tiny_ref: Path, tiny_cache):
    root = tmp_path / "repo"
    write(root / "src" / "indus" / "ok.py", "def add(a, b):\n    return a + b\n")
    write(root / "README.md", "Indus Agent keeps its own words here.\n")
    result = run_sim(root, tiny_ref)
    assert result.code == 0, result.violations


def test_allow_list_suppresses_and_requires_reason(tmp_path: Path, tiny_ref: Path, tiny_cache):
    root = tmp_path / "repo"
    write(root / "src" / "copy.py", REF_PY)
    allow = write(tmp_path / "allow.json", json.dumps([{"path": "src/*.py", "reason": "test"}]))
    assert similarity.run(Context(root=root, ref=tiny_ref), allow).code == 0
    write(allow, json.dumps([{"path": "src/*.py"}]))
    assert similarity.run(Context(root=root, ref=tiny_ref), allow).code == 2


def test_fixtures_folder_is_not_product(tmp_path: Path):
    write(tmp_path / "tooling" / "checks" / "tests" / "fixtures" / "x.py", "a = 1\n")
    write(tmp_path / "tooling" / "checks" / "y.py", "a = 1\n")
    assert similarity.product_files(tmp_path) == ["tooling/checks/y.py"]


def test_index_is_cached(tmp_path: Path, tiny_ref: Path, tiny_cache: Path):
    root = tmp_path / "repo"
    write(root / "src" / "a.py", "x = 1\n")
    first = run_sim(root, tiny_ref)
    second = run_sim(root, tiny_ref)
    assert any("built" in n for n in first.notes)
    assert any("from cache" in n for n in second.notes)
    assert list(tiny_cache.glob("*-gate.pickle"))


def test_closest_ranks_without_gating(tmp_path: Path, tiny_ref: Path, tiny_cache):
    lines = REF_PY.splitlines()
    root = tmp_path / "repo"
    # 3 copied meaningful lines: below the gate, visible to the probe.
    write(root / "src" / "near.py", "\n".join(ln.strip() for ln in lines[8:11]) + "\n")
    result = run_sim(root, tiny_ref, closest=5)
    assert result.code == 0
    top = result.data["closest"]
    assert top and top[0]["score"] < 1.0
    assert top == sorted(top, key=lambda h: -h["score"])


def test_missing_reference_warns_or_fails(tmp_path: Path):
    ctx = Context(root=tmp_path, ref=tmp_path / "nope")
    assert similarity.run(ctx).code == 0
    ctx.require_ref = True
    assert similarity.run(ctx).code == 2


# --------------------------------------------------------------------------- real reference


SKIP_TOKENS = (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT)


def _python_slice(ref: Path, want: int = 60) -> str:
    """Whole lines from a top-level function of a reference file holding >= want tokens."""
    for path in sorted((ref / "agent").rglob("*.py")) + sorted(ref.rglob("*.py")):
        if any(part in ("node_modules", ".git") for part in path.parts):
            continue
        src = path.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        lines = src.splitlines(keepends=True)
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef) or node.end_lineno is None:
                continue
            if node.end_lineno - node.lineno < 15:
                continue
            chunk = "".join(lines[node.lineno - 1 : node.end_lineno])
            count = 0
            toks = tokenize.generate_tokens(io.StringIO(chunk).readline)
            for tok in toks:
                if tok.type == tokenize.NEWLINE and count >= want:
                    return "".join(chunk.splitlines(keepends=True)[: tok.end[0]])
                if tok.type not in SKIP_TOKENS:
                    count += 1
    pytest.skip("no suitable reference Python function found")


@pytest.mark.ref
def test_real_reference_code_slice_is_caught(tmp_path: Path, real_ref: Path):
    snippet = _python_slice(real_ref)
    write(tmp_path / "src" / "indus" / "borrowed.py", snippet)
    result = similarity.run(Context(root=tmp_path, ref=real_ref))
    assert result.code == 1
    assert any(h["detector"] == "tokens" for h in result.data["hits"])


@pytest.mark.ref
def test_real_reference_sentence_is_caught(tmp_path: Path, real_ref: Path):
    words: list[str] = []
    for path in sorted(real_ref.glob("*.md")) + sorted((real_ref / "docs").rglob("*.md")):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            found = simtext.WORD.findall(line.lower())
            if len(found) >= 16 and not line.lstrip().startswith(("|", "`", "#")):
                words = found[2:14]
                break
        if words:
            break
    if not words:
        pytest.skip("no long reference sentence found")
    write(tmp_path / "docs" / "notes" / "copied.md", "Our intro, then " + " ".join(words) + ".\n")
    result = similarity.run(Context(root=tmp_path, ref=real_ref))
    assert any(h["detector"] == "prose" and h["run"] >= 12 for h in result.data["hits"])


@pytest.mark.ref
def test_real_reference_clean_file_passes(tmp_path: Path, real_ref: Path):
    write(
        tmp_path / "src" / "indus" / "fresh.py",
        "def zig_total(stones: list[int]) -> int:\n"
        "    # count the stones on even squares twice\n"
        "    return sum(s * (2 if i % 2 == 0 else 1) for i, s in enumerate(stones))\n",
    )
    write(tmp_path / "docs" / "notes" / "fresh.md", "Purple kettles hum beside quiet glaciers.\n")
    result = similarity.run(Context(root=tmp_path, ref=real_ref))
    assert result.code == 0, result.violations


def test_packages_folder_is_product_code(tmp_path: Path):
    write(tmp_path / "packages" / "ui" / "src" / "index.ts", "export const v = 1;\n")
    write(tmp_path / "packages" / "ui" / "node_modules" / "x" / "i.js", "module.exports = 1;\n")
    files = similarity.product_files(tmp_path)
    assert "packages/ui/src/index.ts" in files
    assert not any("node_modules" in f for f in files)
