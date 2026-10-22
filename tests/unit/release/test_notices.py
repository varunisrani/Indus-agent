"""P25-17: THIRD_PARTY_NOTICES.md is generated from the locks and pins, policed, and kept fresh."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from scripts.release import notices

UV_LOCK = """
version = 1

[[package]]
name = "demo"
version = "0.1.0"
source = { editable = "." }
dependencies = [{ name = "alpha" }]

[package.optional-dependencies]
fast = [{ name = "beta", extra = ["speed"] }]

[package.dev-dependencies]
dev = [{ name = "devtool" }]

[[package]]
name = "alpha"
version = "1.0.0"
dependencies = [{ name = "gamma", marker = "sys_platform == 'win32'" }]

[[package]]
name = "beta"
version = "2.0.0"

[package.optional-dependencies]
speed = [{ name = "delta" }]

[[package]]
name = "gamma"
version = "3.0.0"

[[package]]
name = "delta"
version = "4.0.0"

[[package]]
name = "devtool"
version = "9.0.0"
"""

PNPM_LOCK = """---
lockfileVersion: '9.0'
importers:
  .:
    configDependencies: {}
---
lockfileVersion: '9.0'
importers:
  .:
    devDependencies:
      vitest:
        specifier: 1.0.0
        version: 1.0.0
  apps/web:
    dependencies:
      '@demo/ui':
        specifier: workspace:*
        version: link:../../packages/ui
      react-dom:
        specifier: 19.0.0
        version: 19.0.0(react@19.0.0)
    devDependencies:
      typescript:
        specifier: 6.0.0
        version: 6.0.0
packages:
  react-dom@19.0.0: {}
  react@19.0.0: {}
  scheduler@0.27.0: {}
snapshots:
  react-dom@19.0.0(react@19.0.0):
    dependencies:
      react: 19.0.0
      scheduler: 0.27.0
  react@19.0.0: {}
  scheduler@0.27.0: {}
"""

INSTALL_PINS: dict[str, Any] = {
    "schema": 1,
    "release": {"repository": "x/y"},
    "uv": {"version": "0.1.0"},
}
DOCKER_PINS: dict[str, Any] = {
    "base": {"image": "debian", "tag": "slim", "digest": "sha256:" + "0" * 64},
    "bundles": {
        "s6": {"version": "3", "licence": "ISC"},
        "linter": {"version": "1", "licence": "GPL-3.0-only (CI tool only, never shipped)"},
    },
}
CREDIT = "<!-- note -->\n\n## Design reference\n\nWe credit the original.\n"


def _row(licence: str, **extra: str) -> dict[str, str]:
    return {"licence": licence, "source": "manual", **extra}


def _data() -> dict[str, Any]:
    return {
        "binaries": {
            "uv": {"name": "uv", "licence": "MIT OR Apache-2.0"},
            "docker-base": {"licence": "MIT"},
        },
        "sections": [],
        "licences": {
            "Python": {
                "alpha": {"1.0.0": _row("MIT")},
                "beta": {"2.0.0": _row("Apache-2.0")},
                "gamma": {"3.0.0": _row("BSD-3-Clause")},
                "delta": {"4.0.0": _row("ISC")},
            },
            "npm": {
                "react": {"19.0.0": _row("MIT")},
                "react-dom": {"19.0.0": _row("MIT")},
                "scheduler": {"0.27.0": _row("MIT")},
            },
        },
    }


def _policy(**changes: Any) -> notices.Policy:
    base = {
        "allow": {"MIT", "Apache-2.0", "BSD-3-Clause", "ISC", "MPL-2.0"},
        "exceptions": {},
        "obligations": {},
    }
    base.update(changes)
    return notices.Policy(**base)


def _inputs(tmp_path: Path, data: dict[str, Any] | None = None, **changes: Any) -> notices.Inputs:
    texts = tmp_path / "texts"
    texts.mkdir(exist_ok=True)
    for ident in ("MIT", "Apache-2.0", "BSD-3-Clause", "ISC", "MPL-2.0", "GPL-2.0-only"):
        (texts / f"{ident}.txt").write_text(f"the {ident} terms\n", encoding="utf-8")
    fields: dict[str, Any] = {
        "uv_lock": UV_LOCK.replace('"demo"', f'"{notices.PROJECT}"'),
        "pnpm_lock": PNPM_LOCK,
        "install_pins": INSTALL_PINS,
        "docker_pins": DOCKER_PINS,
        "data": data if data is not None else _data(),
        "policy": _policy(),
        "credit": CREDIT,
        "texts_dir": texts,
    }
    fields.update(changes)
    return notices.Inputs(**fields)


# -- the walks ----------------------------------------------------------------------------------


def test_uv_walk_follows_extras_and_markers_and_skips_dev() -> None:
    found = {
        (c.name, c.version): c.used_by
        for c in notices.python_components(UV_LOCK.replace('"demo"', '"indus-agent"'))
    }
    assert found == {
        ("alpha", "1.0.0"): ("base",),
        ("gamma", "3.0.0"): ("base",),  # behind a marker: still shipped on that platform
        ("beta", "2.0.0"): ("extra fast",),
        ("delta", "4.0.0"): ("extra fast",),  # through beta's own extra
    }


def test_pnpm_walk_takes_production_deps_skips_links_and_strips_peers() -> None:
    found = {(c.name, c.version): c.used_by for c in notices.npm_components(PNPM_LOCK)}
    assert found == {
        ("react-dom", "19.0.0"): ("apps/web",),
        ("react", "19.0.0"): ("apps/web",),
        ("scheduler", "0.27.0"): ("apps/web",),
    }


def test_pinned_binaries_skip_ci_only_bundles() -> None:
    found, problems = notices.pinned_components(INSTALL_PINS, DOCKER_PINS, _data())
    assert problems == []
    assert sorted(c.name for c in found) == ["debian", "s6", "uv"]


def test_a_pinned_binary_without_a_licence_row_fails() -> None:
    pins = {**INSTALL_PINS, "newtool": {"version": "1.0"}}
    _found, problems = notices.pinned_components(pins, DOCKER_PINS, _data())
    assert problems == ["pinned binary 'newtool' (scripts/install/pins.json) has no licence row"]


@pytest.mark.parametrize(
    ("expression", "ids"),
    [
        ("MIT", ["MIT"]),
        ("(MIT OR Apache-2.0) AND BSD-3-Clause", ["MIT", "Apache-2.0", "BSD-3-Clause"]),
        ("GPL-2.0-or-later WITH Classpath-exception-2.0", ["GPL-2.0-or-later"]),
        ("LGPL-2.1+", ["LGPL-2.1"]),
    ],
)
def test_licence_ids(expression: str, ids: list[str]) -> None:
    assert notices.licence_ids(expression) == ids


# -- policy -------------------------------------------------------------------------------------


def test_clean_fixture_generates_grouped_file_with_each_text_once(tmp_path: Path) -> None:
    outcome = notices.generate(_inputs(tmp_path))
    assert outcome.problems == []
    text = outcome.text
    assert text.startswith(notices.MARKER + "\n")
    assert "## Design reference" in text and "<!-- note -->" not in text
    assert "### MIT\n" in text and "### MIT OR Apache-2.0\n" in text
    assert text.count("the MIT terms") == 1
    assert "devtool" not in text and "typescript" not in text and "vitest" not in text
    assert "| s6 | 3 | binary | Docker image |" in text


def test_a_fixture_lock_with_an_unknown_licence_fails(tmp_path: Path) -> None:
    data = _data()
    del data["licences"]["Python"]["gamma"]
    outcome = notices.generate(_inputs(tmp_path, data))
    assert outcome.text == ""
    assert any(p.startswith("Python gamma 3.0.0: unknown licence") for p in outcome.problems)


def test_a_row_for_another_version_counts_as_unknown(tmp_path: Path) -> None:
    data = _data()
    data["licences"]["npm"]["scheduler"] = {"0.26.0": _row("MIT")}
    outcome = notices.generate(_inputs(tmp_path, data))
    assert any("npm scheduler 0.27.0: unknown licence" in p for p in outcome.problems)


def test_a_licence_off_the_allow_list_fails_unless_excepted(tmp_path: Path) -> None:
    data = _data()
    data["licences"]["Python"]["delta"]["4.0.0"] = _row("WTFPL")
    (tmp_path / "texts").mkdir()
    (tmp_path / "texts" / "WTFPL.txt").write_text("do what you want\n", encoding="utf-8")
    refused = notices.generate(_inputs(tmp_path, data))
    assert any(
        "delta 4.0.0: licence 'WTFPL' is not on the allow-list" in p for p in refused.problems
    )
    excepted = notices.generate(
        _inputs(tmp_path, data, policy=_policy(exceptions={"delta": {"reason": "dated why"}}))
    )
    assert excepted.problems == []
    assert "Allowed by exception: dated why" in excepted.text


def test_copyleft_needs_an_obligation_note(tmp_path: Path) -> None:
    data = _data()
    data["licences"]["Python"]["alpha"]["1.0.0"] = _row("MPL-2.0")
    missing = notices.generate(_inputs(tmp_path, data))
    assert any("copyleft MPL-2.0 needs an obligation note" in p for p in missing.problems)
    noted = notices.generate(
        _inputs(
            tmp_path, data, policy=_policy(obligations={"MPL-2.0": "unmodified; source online"})
        )
    )
    assert noted.problems == []
    assert "- **MPL-2.0**: unmodified; source online" in noted.text


def test_a_permissive_alternative_needs_no_obligation(tmp_path: Path) -> None:
    data = _data()
    data["licences"]["Python"]["alpha"]["1.0.0"] = _row("MPL-2.0 OR Apache-2.0")
    assert notices.generate(_inputs(tmp_path, data)).problems == []


def test_a_missing_licence_text_fails(tmp_path: Path) -> None:
    data = _data()
    data["licences"]["Python"]["alpha"]["1.0.0"] = _row("Unlicense")
    outcome = notices.generate(
        _inputs(
            tmp_path,
            data,
            policy=_policy(allow={"MIT", "Apache-2.0", "BSD-3-Clause", "ISC", "Unlicense"}),
        )
    )
    assert any(p.startswith("licence text Unlicense: missing") for p in outcome.problems)


def test_refresh_keeps_manual_rows_and_drops_unlocked_versions(tmp_path: Path) -> None:
    data = _data()
    data["licences"]["Python"]["alpha"]["0.9.0"] = _row("MIT")
    fresh, unknown = notices.refresh(_inputs(tmp_path, data))
    assert unknown == []
    assert fresh["licences"]["Python"]["alpha"] == {"1.0.0": _row("MIT")}


# -- the real file ------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def real() -> notices.Outcome:
    return notices.generate(notices.Inputs.from_repo())


def test_real_file_is_fresh(real: notices.Outcome) -> None:
    assert real.problems == []
    assert notices.OUTPUT.read_text(encoding="utf-8") == real.text


def test_check_fails_on_a_stale_file(tmp_path: Path, real: notices.Outcome) -> None:
    stale = tmp_path / "NOTICES.md"
    stale.write_text(real.text.replace("## Obligations", "## Duties"), encoding="utf-8")
    assert notices.main(["--check", "--output", str(stale)]) == 1
    stale.write_text(real.text, encoding="utf-8")
    assert notices.main(["--check", "--output", str(stale)]) == 0


def test_real_file_lists_every_runtime_dependency(real: notices.Outcome) -> None:
    inputs = notices.Inputs.from_repo()
    rows = set(
        re.findall(r"^\| (\S[^|]*?) \| ([^|]+?) \| (Python|npm|binary|image) \|", real.text, re.M)
    )
    listed = {(name, version) for name, version, _kind in rows}
    for comp in notices.python_components(inputs.uv_lock) + notices.npm_components(
        inputs.pnpm_lock
    ):
        assert (comp.name, comp.version) in listed, comp
    pins = json.loads(notices.INSTALL_PINS.read_text(encoding="utf-8"))
    names = {name for name, _v, kind in rows if kind == "binary"}
    assert {"uv", "python-build-standalone", "git-for-windows"} <= names
    assert ("uv", pins["uv"]["version"]) in listed
    for ident in {i for c in real.components for i in notices.licence_ids(c.licence)}:
        assert f"### {ident}\n" in real.text.split("## Licence texts", 1)[1], ident


def test_reference_credit_line_appears_exactly_once(real: notices.Outcome) -> None:
    credit = notices.CREDIT_FILE.read_text(encoding="utf-8")
    (line,) = [ln for ln in credit.splitlines() if ln.startswith("Copyright (c)")]
    assert real.text.count(line) == 1
    assert real.text.index(line) < real.text.index("## What Indus Agent ships")


def test_docker_image_and_desktop_package_embed_the_same_file() -> None:
    dockerfile = (notices.ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"^COPY [^\n]*\bTHIRD_PARTY_NOTICES\.md\b[^\n]* \./$", dockerfile, re.M)
    builder = (notices.ROOT / "apps" / "desktop" / "electron-builder.config.cjs").read_text(
        encoding="utf-8"
    )
    assert '{ from: "../../THIRD_PARTY_NOTICES.md", to: "THIRD_PARTY_NOTICES.md" }' in builder
    ignore = (notices.ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert "THIRD_PARTY_NOTICES.md" not in ignore and "*.md" not in ignore


# -- W46 RS7: refresh writes the layout Biome keeps ----------------------------------------------


def test_dump_json_keeps_the_committed_data_file_byte_for_byte() -> None:
    """The committed file passes Biome; a refresh that changes no row must not reformat it."""
    text = notices.DATA_FILE.read_text(encoding="utf-8")
    assert notices.dump_json(json.loads(text)) == text


def test_dump_json_arrays_fold_to_the_line_width_counting_the_comma() -> None:
    # ``  "k": `` is 7 columns; ["a…", "b"] adds 9 around the first string, the comma 1 more.
    fits = 100 - 7 - 9 - 1
    data = {"k": ["a" * fits, "b"], "z": []}
    assert notices.dump_json(data) == '{\n  "k": ["' + "a" * fits + '", "b"],\n  "z": []\n}\n'
    over = {"k": ["a" * (fits + 1), "b"], "z": []}
    assert notices.dump_json(over) == (
        '{\n  "k": [\n    "' + "a" * (fits + 1) + '",\n    "b"\n  ],\n  "z": []\n}\n'
    )
    last = {"k": ["a" * (fits + 1), "b"]}  # no comma after the last key: the same text fits
    assert notices.dump_json(last) == '{\n  "k": ["' + "a" * (fits + 1) + '", "b"]\n}\n'


def test_dump_json_objects_always_break_and_rows_fold_one_by_one() -> None:
    data = {"t": {"rows": [["pkg", "1.0", "MIT"], ["x" * 95, "2.0"]], "o": [{"a": 1}], "e": {}}}
    assert notices.dump_json(data) == (
        "{\n"
        '  "t": {\n'
        '    "rows": [\n'
        '      ["pkg", "1.0", "MIT"],\n'
        "      [\n"
        '        "' + "x" * 95 + '",\n'
        '        "2.0"\n'
        "      ]\n"
        "    ],\n"
        '    "o": [\n'
        "      {\n"
        '        "a": 1\n'
        "      }\n"
        "    ],\n"
        '    "e": {}\n'
        "  }\n"
        "}\n"
    )
    assert json.loads(notices.dump_json(data)) == data
