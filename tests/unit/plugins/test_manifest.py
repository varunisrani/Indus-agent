"""P13-13: reading plugin manifests (YAML and JSON), warnings never failures, the version gate."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from indus.plugins.manifest import (
    EnvRequirement,
    PluginRequirement,
    manifest_from_mapping,
    read_manifest,
    satisfies,
    version_gate,
)
from tests.unit.plugins._helpers import FIXTURES


def write(folder: Path, name: str, text: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(text, encoding="utf-8")
    return folder


def test_fixture_manifest_reads_every_field() -> None:
    read = read_manifest(FIXTURES / "good", source="user")
    assert read is not None and read.manifest is not None
    m = read.manifest
    assert (m.name, m.version, m.license, m.manifest_version) == ("good", "1.2.0", "MIT", 2)
    assert m.author == "Sample Author, https://example.invalid"
    assert m.tags == ("sample", "test")
    assert m.provides_hooks == ("pre_tool_call", "post_tool_call")
    assert (m.source, m.key, m.plugin_id, m.category) == ("user", "good", "good", "")
    assert m.path == str(FIXTURES / "good")
    assert read.warnings == ()


def test_unknown_field_warns_and_is_ignored(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    folder = write(tmp_path / "odd", "plugin.yaml", "name: odd\nflavour: mint\n")
    with caplog.at_level(logging.WARNING):
        read = read_manifest(folder, source="user")
    assert read is not None and read.manifest is not None
    assert read.manifest.name == "odd"
    assert any("flavour" in w for w in read.warnings)
    assert "flavour" in caplog.text


def test_missing_name_comes_from_the_folder(tmp_path: Path) -> None:
    folder = write(tmp_path / "nameless", "plugin.yaml", "version: 1.0\n")
    read = read_manifest(folder, source="user")
    assert read is not None and read.manifest is not None
    assert read.manifest.name == "nameless"
    assert read.manifest.key == "nameless"
    assert any("no name" in w for w in read.warnings)


def test_newer_manifest_version_warns(tmp_path: Path) -> None:
    folder = write(tmp_path / "future", "plugin.yaml", "name: future\nmanifest_version: 3\n")
    read = read_manifest(folder, source="user")
    assert read is not None and read.manifest is not None
    assert read.manifest.manifest_version == 3
    assert any("manifest_version 3" in w for w in read.warnings)


def test_wrong_types_fall_back_with_warnings(tmp_path: Path) -> None:
    text = (
        "name: typed\n"
        "kind: gadget\n"
        "tags: just-one-string\n"
        "manifest_version: two\n"
        "config_schema: [1, 2]\n"
        "requires_env: [OK_NAME, {prompt: missing name}]\n"
    )
    read = read_manifest(write(tmp_path / "typed", "plugin.yaml", text), source="user")
    assert read is not None and read.manifest is not None
    m = read.manifest
    assert m.kind == "standalone"
    assert m.tags == ()
    assert m.manifest_version == 1
    assert m.config_schema == {}
    assert m.requires_env == ()
    joined = " ".join(read.warnings)
    for name in ("kind", "tags", "manifest_version", "config_schema", "requires_env"):
        assert name in joined


def test_requires_plugins_shapes() -> None:
    manifest, warnings = manifest_from_mapping(
        {
            "name": "x",
            "requires_plugins": ["base", {"name": "extra", "version": ">=1.2"}],
            "requires_env": ["PLAIN", {"name": "TOKEN_NAME", "secret": True}],
        },
        fallback_name="x",
    )
    assert warnings == []
    assert manifest.requires_plugins == (
        PluginRequirement(name="base"),
        PluginRequirement(name="extra", version=">=1.2"),
    )
    assert manifest.requires_env == ("PLAIN", EnvRequirement(name="TOKEN_NAME", secret=True))
    assert manifest.env_names() == ["PLAIN", "TOKEN_NAME"]


def test_yaml_and_json_forms_are_equal(tmp_path: Path) -> None:
    data = {
        "name": "twin",
        "version": "0.4.1",
        "description": "same either way",
        "kind": "backend",
        "capabilities": ["tools.override"],
        "requires_plugins": [{"name": "base", "version": ">=1"}],
        "requires_indus": ">=0.0.1",
        "emits": ["ready"],
    }
    yaml_text = "\n".join(f"{k}: {json.dumps(v)}" for k, v in data.items())
    from_yaml = read_manifest(
        write(tmp_path / "a" / "twin", "plugin.yaml", yaml_text), source="user"
    )
    from_json = read_manifest(
        write(tmp_path / "b" / "twin", "plugin.json", json.dumps(data)), source="user"
    )
    assert from_yaml is not None and from_json is not None
    assert from_yaml.manifest is not None and from_json.manifest is not None
    assert from_yaml.manifest.model_copy(update={"path": ""}) == from_json.manifest.model_copy(
        update={"path": ""}
    )


def test_fixture_json_manifest(tmp_path: Path) -> None:
    read = read_manifest(FIXTURES / "capabilities", source="user")
    assert read is not None and read.manifest is not None
    assert read.manifest.capabilities == ("tools.override", "platform.actions")
    assert read.manifest.requires_env == (
        EnvRequirement(name="SAMPLE_SERVICE_KEY", prompt="Key for the sample service", secret=True),
    )


def test_no_manifest_is_not_a_plugin(tmp_path: Path) -> None:
    (tmp_path / "plain").mkdir()
    assert read_manifest(tmp_path / "plain", source="user") is None


@pytest.mark.parametrize(
    ("text", "why"),
    [("name: [unclosed\n", "could not be read"), ("- a\n- b\n", "mapping")],
)
def test_unreadable_manifest_is_an_error(tmp_path: Path, text: str, why: str) -> None:
    read = read_manifest(write(tmp_path / "broken", "plugin.yaml", text), source="user")
    assert read is not None
    assert read.manifest is None
    assert read.error is not None and why in read.error


def test_category_prefix_makes_the_key(tmp_path: Path) -> None:
    folder = write(tmp_path / "tts" / "voice", "plugin.yaml", "name: fancy-voice\n")
    read = read_manifest(folder, source="bundled", prefix="tts")
    assert read is not None and read.manifest is not None
    assert (read.manifest.key, read.manifest.category, read.manifest.name) == (
        "tts/voice",
        "tts",
        "fancy-voice",
    )


@pytest.mark.parametrize(
    ("spec", "version", "ok"),
    [
        (">=0.2", "0.2.0", True),
        (">=0.2", "0.1.9", False),
        ("0.3", "0.2.9", False),  # a bare version is a floor
        ("0.3", "1.0.0", True),
        (">=1.0, <2.0", "1.5.0", True),
        (">=1.0, <2.0", "2.0.0", False),
        ("==1.2", "1.2.0", True),
        ("!=1.2", "1.2.0", False),
        (">1.2", "1.2.1rc1", True),
        ("~=1.2", "0.1.0", True),  # not understood: never blocks
        (">=banana", "0.1.0", True),
        (">=1.0", "unknown", True),
    ],
)
def test_version_gate_table(spec: str, version: str, ok: bool) -> None:
    assert satisfies(spec, version) is ok
    manifest, _ = manifest_from_mapping({"name": "g", "requires_indus": spec}, fallback_name="g")
    reason = version_gate(manifest, version)
    assert (reason is None) is ok
    if not ok:
        assert reason is not None and spec in reason and version in reason
