"""``skills_list`` / ``skill_view`` progressive disclosure (P09-08)."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.scope import Scope
from indus.learning.skills import view_tools
from indus.learning.skills.resolver import clear_scan_cache
from indus.learning.skills.usage import usage_for
from indus.learning.skills.view_tools import (
    LIST_CACHE_TTL_S,
    READS,
    clear_list_cache,
    content_hash,
    skill_view,
    skills_list,
)
from indus.protocol.envelope import ErrorObject
from indus.protocol.requests_port import RequestUnsupported
from indus.protocol.server_requests import SecretAnswer
from indus.safety import child_env
from indus.tools.registry import ToolRegistry
from tests.fakes.clock import FakeClock
from tests.unit.learning.skills.helpers import make_skill, with_skills_config

ENV_SKILL_EXTRA = (
    "required_environment_variables:\n"
    "  - name: DEMO_SERVICE_TOKEN\n"
    "    prompt: Token for the demo service\n"
)


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    clear_scan_cache()
    clear_list_cache()
    READS.clear()
    yield
    child_env.clear_passthrough()


def view(scope: Scope, **kwargs: Any) -> dict[str, Any]:
    extra = {k: kwargs.pop(k) for k in ("session_id", "broker", "run_context") if k in kwargs}
    return json.loads(asyncio.run(skill_view(kwargs, scope=scope, **extra)))


def listing(scope: Scope, **args: Any) -> dict[str, Any]:
    return json.loads(skills_list(args, scope=scope))


class FakeBroker:
    def __init__(self, value: str | None = None, unsupported: bool = False) -> None:
        self.value = value
        self.unsupported = unsupported
        self.asked: list[tuple[str, Mapping[str, Any]]] = []

    async def ask(self, kind: str, payload: Mapping[str, Any], **_: Any) -> SecretAnswer:
        self.asked.append((kind, payload))
        if self.unsupported:
            raise RequestUnsupported(kind, ErrorObject(code=-32601, message="cannot answer"))
        return SecretAnswer(value=self.value)


def test_list_rows_and_category_filter(scope: Scope) -> None:
    root = scope.home / "skills"
    make_skill(root, "dev/git-helper", description="Branch and commit.")
    make_skill(root, "notes", description="Write notes.")
    rows = listing(scope)["skills"]
    assert rows == [
        {"name": "notes", "description": "Write notes.", "category": None},
        {"name": "git-helper", "description": "Branch and commit.", "category": "dev"},
    ]
    assert [r["name"] for r in listing(scope, category="dev")["skills"]] == ["git-helper"]
    assert "hint" in listing(scope, category="none")


def test_list_cache_ttl(scope: Scope, fake_clock: FakeClock) -> None:
    root = scope.home / "skills"
    make_skill(root, "one")
    assert listing(scope)["count"] == 1
    make_skill(root, "two")
    fake_clock.advance(LIST_CACHE_TTL_S - 1)
    assert listing(scope)["count"] == 1  # still cached
    fake_clock.advance(2)
    assert listing(scope)["count"] == 2  # refreshed after 30 s
    make_skill(root, "three")
    clear_list_cache(scope)
    assert listing(scope)["count"] == 3


def test_list_ignores_tool_conditions_but_not_disabled(scope: Scope) -> None:
    root = scope.home / "skills"
    make_skill(root, "needs-tool", extra="metadata:\n  indus:\n    requires_tools: [browser]\n")
    make_skill(root, "turned-off")
    scoped = with_skills_config(scope, disabled=["turned-off"])
    assert [r["name"] for r in listing(scoped)["skills"]] == ["needs-tool"]


def test_view_skill_md(scope: Scope) -> None:
    folder = make_skill(scope.home / "skills", "demo", description="Demo skill.")
    (folder / "references").mkdir()
    (folder / "references" / "api.md").write_text("API notes")
    (folder / "scripts").mkdir()
    (folder / "scripts" / "run.sh").write_text("echo hi")
    out = view(scope, name="demo", session_id="s1")
    assert out["name"] == "demo" and out["content"].startswith("---\nname: demo")
    assert out["metadata"]["description"] == "Demo skill."
    assert out["linked_files"] == {
        "references": ["references/api.md"],
        "scripts": ["scripts/run.sh"],
    }
    assert READS.fresh("s1", "demo", "SKILL.md", (folder / "SKILL.md").read_bytes())
    assert not READS.fresh("s2", "demo", "SKILL.md", (folder / "SKILL.md").read_bytes())


def test_view_nested_support_file(scope: Scope) -> None:
    folder = make_skill(scope.home / "skills", "demo")
    nested = folder / "templates" / "deep" / "x.yaml"
    nested.parent.mkdir(parents=True)
    nested.write_text("key: value\n")
    out = view(scope, name="demo", file_path="templates/deep/x.yaml", session_id="s1")
    assert out == {"name": "demo", "file_path": "templates/deep/x.yaml", "content": "key: value\n"}
    assert READS.fresh("s1", "demo", "templates/deep/x.yaml", b"key: value\n")
    assert not READS.fresh("s1", "demo", "templates/deep/x.yaml", b"changed")


def test_escape_refused(scope: Scope, tmp_path: Path) -> None:
    folder = make_skill(scope.home / "skills", "demo")
    secret = tmp_path / "outside.txt"
    secret.write_text("private")
    (folder / "references").mkdir()
    os.symlink(secret, folder / "references" / "link.md")
    for path in ("../demo/SKILL.md", "../../config.yaml", "references/link.md", str(secret)):
        out = view(scope, name="demo", file_path=path)
        assert "outside the skill folder" in out["error"], path


def test_missing_support_file_lists_existing(scope: Scope) -> None:
    folder = make_skill(scope.home / "skills", "demo")
    (folder / "references").mkdir()
    (folder / "references" / "a.md").write_text("a")
    out = view(scope, name="demo", file_path="references/b.md")
    assert out["available_files"] == ["references/a.md"]


def test_binary_support_file(scope: Scope) -> None:
    folder = make_skill(scope.home / "skills", "demo")
    (folder / "assets").mkdir()
    (folder / "assets" / "logo.png").write_bytes(b"\x89PNG\xff\xfe\x00")
    out = view(scope, name="demo", file_path="assets/logo.png")
    assert out["binary"] is True and out["size"] == 7 and "content" not in out


def test_view_bumps_counter(scope: Scope) -> None:
    make_skill(scope.home / "skills", "demo")
    view(scope, name="demo")
    view(scope, name="demo")
    assert usage_for(scope).get("demo")["view_count"] == 2


def test_disabled_and_unknown_refused(scope: Scope) -> None:
    make_skill(scope.home / "skills", "turned-off")
    scoped = with_skills_config(scope, disabled=["turned-off"])
    assert "disabled" in view(scoped, name="turned-off")["error"]
    assert "no skill named" in view(scope, name="nope")["error"]
    assert "required" in view(scope, name="")["error"]


def test_missing_env_prompts_on_cli(scope: Scope) -> None:
    make_skill(scope.home / "skills", "envy", extra=ENV_SKILL_EXTRA)
    broker = FakeBroker(value="v-" + "123")
    out = view(scope, name="envy", broker=broker, run_context="interactive")
    assert out["setup"] == {"stored": ["DEMO_SERVICE_TOKEN"]}
    assert broker.asked[0][0] == "secret"
    assert broker.asked[0][1]["name"] == "DEMO_SERVICE_TOKEN"
    assert scope.secrets is not None and scope.secrets.get("DEMO_SERVICE_TOKEN") == "v-123"
    assert "DEMO_SERVICE_TOKEN" in child_env.passthrough_names(scope)[0]
    again = FakeBroker(value="other")
    assert "setup" not in view(scope, name="envy", broker=again, run_context="interactive")
    assert again.asked == []


def test_missing_env_skipped_gives_hint(scope: Scope) -> None:
    make_skill(scope.home / "skills", "envy", extra=ENV_SKILL_EXTRA)
    out = view(scope, name="envy", broker=FakeBroker(value=None), run_context="interactive")
    assert out["setup"]["missing"] == ["DEMO_SERVICE_TOKEN"]


def test_missing_env_hint_on_gateway(scope: Scope) -> None:
    make_skill(scope.home / "skills", "envy", extra=ENV_SKILL_EXTRA)
    broker = FakeBroker(unsupported=True)
    out = view(scope, name="envy", broker=broker, run_context="interactive")
    assert out["setup"]["missing"] == ["DEMO_SERVICE_TOKEN"]
    assert "local terminal" in out["setup"]["hint"]


def test_cron_never_prompts(scope: Scope) -> None:
    make_skill(scope.home / "skills", "envy", extra=ENV_SKILL_EXTRA)
    broker = FakeBroker(value="x")
    out = view(scope, name="envy", broker=broker, run_context="cron")
    assert broker.asked == [] and out["setup"]["missing"] == ["DEMO_SERVICE_TOKEN"]


def test_env_present_in_scope_env(scope: Scope) -> None:
    make_skill(scope.home / "skills", "envy", extra=ENV_SKILL_EXTRA)
    scoped = scope.with_env(scope.env.with_overlay({"DEMO_SERVICE_TOKEN": "set"}))
    assert "setup" not in view(scoped, name="envy")


def test_registered_read_only(scope: Scope) -> None:
    registry = ToolRegistry()
    view_tools.register_tools(registry)
    for name in ("skills_list", "skill_view"):
        entry = registry.get(name)
        assert entry is not None and entry.toolset == "skills" and entry.traits.read_only
    assert content_hash("a") == content_hash(b"a")
