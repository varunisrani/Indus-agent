"""W48-g-2: wire-model field docstrings come from a generated twin, not a source parse per class."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

from indus.protocol import base, field_docs

_PROBE = (
    "import ast, json, sys\n"
    "parsed = []\n"
    "real = ast.parse\n"
    "ast.parse = lambda *a, **k: parsed.append(1) or real(*a, **k)\n"
    "import indus.protocol.events, indus.protocol.methods\n"
    "from indus.protocol.methods import SessionCreateParams as Model\n"
    "print(json.dumps({'parsed': len(parsed), 'cwd': Model.model_fields['cwd'].description}))\n"
)


def test_the_checked_in_twin_is_fresh() -> None:
    # Regenerate with `uv run python -m indus.protocol.field_docs` after editing a wire model.
    assert field_docs.main(["--check"]) == 0


def test_importing_the_wire_models_parses_no_source() -> None:
    done = subprocess.run(
        [sys.executable, "-c", _PROBE], capture_output=True, text=True, timeout=120, check=True
    )
    seen = json.loads(done.stdout.strip().splitlines()[-1])
    assert seen["parsed"] == 0
    assert seen["cwd"]


def test_a_module_missing_from_the_twin_or_changed_since_is_parsed(tmp_path: Path) -> None:
    source = 'class Probe:\n    name: str\n    """A name."""\n'
    module = tmp_path / "probe.py"
    module.write_text(source, encoding="utf-8")
    assert base.module_docs("not.in.the.twin", str(module)) == {1: {"name": "A name."}}
    events = Path(base.__file__).with_name("events.py")
    twin = json.loads(base.DOCS_TWIN.read_text(encoding="utf-8"))
    assert twin["modules"]["indus.protocol.events"]["sha256"] == base.source_digest(
        events.read_bytes()
    )
    assert base.module_docs("indus.protocol.events", str(module)) == base.class_docs(
        ast.parse(source)
    )
