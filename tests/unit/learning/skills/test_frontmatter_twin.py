"""W50-f-2: the shipped skills' front matter comes from a generated twin keyed by block digest."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from indus.foundation import yamlio
from indus.learning.skills import format as fmt
from indus.learning.skills import frontmatter_twin
from indus.learning.skills.bundled_sync import bundled_source, discover


def _shipped() -> list[tuple[str, str]]:
    return [
        (skill.rel.as_posix(), (skill.path / fmt.SKILL_FILE).read_text(encoding="utf-8"))
        for skill in discover(bundled_source({}))
    ]


@pytest.fixture
def yaml_calls(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    calls: list[str] = []
    real = yamlio.load_safe

    def counting(source: str | bytes | Path) -> object:
        calls.append(str(source))
        return real(source)

    monkeypatch.setattr(yamlio, "load_safe", counting)
    fmt._shipped_blocks.cache_clear()  # pyright: ignore[reportPrivateUsage]
    yield calls
    fmt._shipped_blocks.cache_clear()  # pyright: ignore[reportPrivateUsage]


def test_the_checked_in_twin_is_fresh() -> None:
    # Regenerate with `uv run python -m indus.learning.skills.frontmatter_twin` after editing a
    # shipped skill.
    assert frontmatter_twin.main(["--check"]) == 0


def test_the_twin_covers_every_shipped_skill() -> None:
    twin = json.loads(fmt.SHIPPED_TWIN.read_text(encoding="utf-8"))
    shipped = _shipped()
    assert len(shipped) >= 15
    assert sorted(twin["skills"]) == sorted(rel for rel, _ in shipped)
    assert twin["skipped"] == []


def test_shipped_skills_parse_from_the_twin_without_yaml(yaml_calls: list[str]) -> None:
    for rel, text in _shipped():
        parsed = fmt.parse_skill(text)
        block, _ = fmt.frontmatter_block(text)
        assert block is not None
        assert parsed.raw == yamlio.load_safe(block), rel
    # only the comparisons above parsed YAML; parse_skill itself never did
    assert len(yaml_calls) == len(_shipped())


def test_an_edited_block_is_parsed(yaml_calls: list[str]) -> None:
    _, text = _shipped()[0]
    edited = text.replace("---\n", "---\nextra_key: 1\n", 1)
    raw, _ = fmt.split_frontmatter(edited)
    assert raw["extra_key"] == 1
    assert len(yaml_calls) == 1


def test_each_hit_is_an_independent_copy(yaml_calls: list[str]) -> None:
    _, text = _shipped()[0]
    first, _ = fmt.split_frontmatter(text)
    first["name"] = "changed"
    second, _ = fmt.split_frontmatter(text)
    assert second["name"] != "changed"
    assert yaml_calls == []


@pytest.mark.parametrize("twin", [None, "not json", '{"skills": []}', '{"skills": {"a": 1}}'])
def test_an_unusable_twin_means_every_block_is_parsed(
    twin: str | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, yaml_calls: list[str]
) -> None:
    path = tmp_path / "shipped_frontmatter.json"
    if twin is not None:
        path.write_text(twin, encoding="utf-8")
    monkeypatch.setattr(fmt, "SHIPPED_TWIN", path)
    rel, text = _shipped()[0]
    assert fmt.parse_skill(text).raw == yamlio.load_safe(fmt.frontmatter_block(text)[0] or "")
    assert len(yaml_calls) == 2, rel


def test_front_matter_json_cannot_carry_is_skipped(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    fronts = {
        "plain": "name: plain\ndescription: ok",
        "dated": "name: dated\ndescription: ok\ncreated: 2026-01-02",  # a YAML date
    }
    for name, front in fronts.items():
        folder = root / "cat" / name
        folder.mkdir(parents=True)
        (folder / fmt.SKILL_FILE).write_text(f"---\n{front}\n---\nBody.\n", encoding="utf-8")
    skills, skipped = frontmatter_twin.collect(root)
    assert sorted(skills) == ["cat/plain"]
    assert skipped == ["cat/dated"]


def test_plain_json() -> None:
    assert frontmatter_twin.plain_json({"a": [1, 2.5, "x", None, True]})
    assert not frontmatter_twin.plain_json({"a": float("nan")})
    assert not frontmatter_twin.plain_json({1: "x"})
    assert not frontmatter_twin.plain_json({"a": (1,)})
