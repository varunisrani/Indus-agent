"""Shared builders for the skill tests: skill folders, config views and scopes."""

from __future__ import annotations

import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from indus.foundation.scope import Scope

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "skills"


def skill_md(
    name: str, description: str = "", body: str = "Steps go here.\n", extra: str = ""
) -> str:
    desc = description or f"The {name} skill."
    return f"---\nname: {name}\ndescription: {desc}\n{extra}---\n\n{body}"


def make_skill(
    root: Path,
    rel: str,
    name: str | None = None,
    description: str = "",
    body: str = "Steps go here.\n",
    extra: str = "",
) -> Path:
    folder = root / rel
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(
        skill_md(name or Path(rel).name, description, body, extra), encoding="utf-8"
    )
    return folder


class FakeConfig:
    """A config view over a nested dict with dotted ``get``."""

    def __init__(self, tree: Mapping[str, Any] | None = None) -> None:
        self.tree: dict[str, Any] = dict(tree or {})

    def get(self, path: str, default: Any = None) -> Any:
        node: Any = self.tree
        for part in path.split("."):
            if not isinstance(node, Mapping) or part not in node:
                return default
            node = node[part]  # pyright: ignore[reportUnknownVariableType]
        return node  # pyright: ignore[reportUnknownVariableType]


def with_skills_config(scope: Scope, **skills: Any) -> Scope:
    return scope.with_config(FakeConfig({"skills": skills}))


def copy_tiers(dest: Path) -> dict[str, Path]:
    """Copy the tier fixture tree to ``dest``; return its ``local``/``create``/``external``."""
    shutil.copytree(FIXTURES / "tiers", dest)
    return {name: dest / name for name in ("local", "create", "external")}


def record_hub(skills_root: Path, name: str, trust: str = "community", path: str = "") -> None:
    """A hub lock entry in the P13 shape (``skills.hub.lock``), as an install would write it."""
    from indus.learning.skills.hub.lock import HubLock

    HubLock(skills_root).record(
        {
            "name": name,
            "source": "url",
            "identifier": f"https://hub.example/{name}/SKILL.md",
            "install_path": path or name,
            "content_hash": "",
            "upstream_hash": "",
            "trust": trust,
            "verdict": "safe",
            "scanner_version": "indus-scan-v1",
            "findings": [],
            "files": ["SKILL.md"],
            "ts": "2026-10-19T00:00:00+00:00",
        }
    )
