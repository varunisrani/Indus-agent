"""Pin policy: every rule on a synthetic repo, the real repo, and the CI gate's shape."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import date
from pathlib import Path

import pytest

from tooling.checks import pins
from tooling.checks.common import DEFAULT_ROOT, Context, Result
from tooling.checks.tests.conftest import write

TODAY = date(2026, 10, 18)
SHA = "0123456789abcdef0123456789abcdef01234567"
CI = DEFAULT_ROOT / ".github" / "workflows" / "ci.yml"


def pyproject(deps: list[str], *, exclude_newer: str | None = "2026-10-04T00:00:00Z") -> str:
    lines = ["[project]", 'name = "demo"', f"dependencies = {json.dumps(deps)}", ""]
    if exclude_newer is not None:
        lines += ["[tool.uv]", f'exclude-newer = "{exclude_newer}"', ""]
    return "\n".join(lines)


def check(
    root: Path,
    *,
    allow: dict | None = None,
    lock_date: str = "2026-10-18",
) -> Result:
    policy = root / "_policy"
    write(policy / "pins_allow.json", json.dumps({"allow": allow or {}}))
    write(policy / "pins_meta.json", json.dumps({"lock_date": lock_date}))
    return pins.run(
        Context(root=root),
        allow_path=policy / "pins_allow.json",
        meta_path=policy / "pins_meta.json",
        today=TODAY,
    )


def repo(tmp_path: Path, deps: list[str], **kw) -> Path:
    write(tmp_path / "pyproject.toml", pyproject(deps, **kw))
    return tmp_path


# --------------------------------------------------------------------------- rule 1: Python pins


def test_open_range_fails(tmp_path: Path):
    result = check(repo(tmp_path, ["httpx>=0.27"]))
    assert result.code == 1
    assert "httpx>=0.27" in result.violations[0] and "httpx==X.Y.Z" in result.violations[0]


@pytest.mark.parametrize("req", ["httpx==0.28.1", "httpx[http2]==0.28.1", "import-linter==2.15"])
def test_exact_pin_passes(tmp_path: Path, req: str):
    result = check(repo(tmp_path, [req]))
    assert result.code == 0, result.violations


@pytest.mark.parametrize("req", ["httpx~=0.28", "httpx==0.*", "httpx", "httpx===0.28.1", "x==1"])
def test_other_specs_fail(tmp_path: Path, req: str):
    assert check(repo(tmp_path, [req])).code == 1


def test_bounded_range_needs_allow_entry(tmp_path: Path):
    root = repo(tmp_path, ["foo>=1,<2"])
    assert check(root).code == 1
    allowed = check(root, allow={"foo": {"spec": ">=1,<2", "reason": "plugin API floats"}})
    assert allowed.code == 0, allowed.violations


def test_allow_entry_without_reason_is_setup_error(tmp_path: Path):
    result = check(repo(tmp_path, ["foo>=1,<2"]), allow={"foo": {"spec": ">=1,<2"}})
    assert result.code == 2 and "reason" in result.violations[0]


def test_allow_entry_must_be_bounded(tmp_path: Path):
    result = check(repo(tmp_path, ["foo>=1"]), allow={"foo": {"spec": ">=1", "reason": "x"}})
    assert result.code == 2


def test_range_wider_than_allowed_fails(tmp_path: Path):
    allow = {"foo": {"spec": ">=1,<2", "reason": "x"}}
    assert check(repo(tmp_path, ["foo>=1,<3"]), allow=allow).code == 1


def test_unused_allow_entry_warns(tmp_path: Path):
    result = check(repo(tmp_path, []), allow={"foo": {"spec": ">=1,<2", "reason": "x"}})
    assert result.code == 0 and result.warnings


def test_interpreter_marker_fails(tmp_path: Path):
    result = check(repo(tmp_path, ["httpx==0.28.1; python_version>='3.14'"]))
    assert result.code == 1 and "python_version" in result.violations[0]


def test_platform_markers_pass(tmp_path: Path):
    req = "pywin==1.0.0; sys_platform == 'win32' and platform_machine == 'AMD64'"
    assert check(repo(tmp_path, [req])).code == 0


def test_groups_and_extras_are_covered(tmp_path: Path):
    write(
        tmp_path / "pyproject.toml",
        '[project]\nname = "demo"\ndependencies = []\n'
        '[project.optional-dependencies]\nweb = ["ddgs>=9"]\n'
        '[dependency-groups]\ndev = ["pytest==9.1.1", {include-group = "lint"}]\n'
        'lint = ["ruff"]\n'
        '[tool.uv]\nexclude-newer = "2026-10-04T00:00:00Z"\n',
    )
    result = check(tmp_path)
    assert result.code == 1
    text = "\n".join(result.violations)
    assert "optional-dependencies].web" in text and "dependency-groups].lint" in text
    assert "pytest" not in text


# --------------------------------------------------------------------------- rule 2: age window


def test_missing_exclude_newer_fails(tmp_path: Path):
    result = check(repo(tmp_path, [], exclude_newer=None))
    assert result.code == 1 and "exclude-newer is missing" in result.violations[0]


def test_three_day_window_fails(tmp_path: Path):
    result = check(repo(tmp_path, [], exclude_newer="2026-10-15T00:00:00Z"))
    assert result.code == 1 and "2026-10-04T00:00:00Z" in result.violations[0]


def test_exactly_fourteen_days_passes(tmp_path: Path):
    assert check(repo(tmp_path, [], exclude_newer="2026-10-04T00:00:00Z")).code == 0


@pytest.mark.parametrize("value", ["2026-10-01", "14 days", "2026-10-01T00:00:00"])
def test_exclude_newer_must_be_rfc3339(tmp_path: Path, value: str):
    assert check(repo(tmp_path, [], exclude_newer=value)).code == 1


def test_lock_date_in_future_fails(tmp_path: Path):
    result = check(repo(tmp_path, [], exclude_newer="2026-10-20T00:00:00Z"), lock_date="2026-11-03")
    assert result.code == 1 and "future" in result.violations[0]


def test_bad_meta_is_setup_error(tmp_path: Path):
    assert check(repo(tmp_path, []), lock_date="soon").code == 2


def test_young_locked_file_fails(tmp_path: Path):
    root = repo(tmp_path, [])
    write(
        root / "uv.lock",
        '[[package]]\nname = "old"\nsdist = { upload-time = "2026-09-01T00:00:00Z" }\n'
        '[[package]]\nname = "fresh"\nwheels = [{ upload-time = "2026-10-10T08:00:00.5Z" }]\n',
    )
    result = check(root)
    assert result.code == 1
    assert len(result.violations) == 1 and "fresh" in result.violations[0]


# --------------------------------------------------------------------------- rules 3-4: Node


def node_repo(tmp_path: Path, deps: dict, *, age: str | None = "20160", field="dependencies"):
    root = repo(tmp_path, [])
    write(root / "packages" / "ui" / "package.json", json.dumps({"name": "@x/ui", field: deps}))
    if age is not None:
        write(
            root / "pnpm-workspace.yaml", f"packages:\n  - packages/*\nminimumReleaseAge: {age}\n"
        )
    return root


@pytest.mark.parametrize("field", ["dependencies", "devDependencies", "optionalDependencies"])
@pytest.mark.parametrize("spec", ["^1.0.0", "~1.0.0", "*", "1.x", "latest", ">=1.0.0", "1.0"])
def test_node_ranges_fail(tmp_path: Path, field: str, spec: str):
    result = check(node_repo(tmp_path, {"left-pad": spec}, field=field))
    assert result.code == 1 and "packages/ui/package.json" in result.violations[0]


def test_node_exact_and_workspace_pass(tmp_path: Path):
    deps = {"vitest": "5.0.3", "@indus/protocol": "workspace:*", "x": "1.0.0-beta.2"}
    assert check(node_repo(tmp_path, deps)).code == 0


def test_workspace_only_for_own_scope(tmp_path: Path):
    assert check(node_repo(tmp_path, {"other": "workspace:*"})).code == 1
    assert check(node_repo(tmp_path, {"@indus/ui": "workspace:^"})).code == 1


def test_peer_dependencies_exempt(tmp_path: Path):
    assert check(node_repo(tmp_path, {"react": "^19.0.0"}, field="peerDependencies")).code == 0


def test_node_modules_ignored(tmp_path: Path):
    root = node_repo(tmp_path, {})
    write(root / "node_modules" / "x" / "package.json", json.dumps({"dependencies": {"a": "^1"}}))
    assert check(root).code == 0


def test_release_age_below_14_days_fails(tmp_path: Path):
    result = check(node_repo(tmp_path, {}, age="1440"))
    assert result.code == 1 and "minimumReleaseAge" in result.violations[0]


def test_missing_workspace_file_fails(tmp_path: Path):
    assert check(node_repo(tmp_path, {}, age=None)).code == 1


# --------------------------------------------------------------------------- rule 5: git sources


def test_git_requirement_with_branch_fails_and_sha_passes(tmp_path: Path):
    url = "foo @ git+https://example.com/foo.git"
    assert check(repo(tmp_path, [f"{url}@main"])).code == 1
    assert check(repo(tmp_path, [url])).code == 1
    assert check(repo(tmp_path, [f"{url}@{SHA}"])).code == 0


def test_non_git_url_fails(tmp_path: Path):
    assert check(repo(tmp_path, ["foo @ https://example.com/foo-1.0.tar.gz"])).code == 1


@pytest.mark.parametrize(
    ("source", "code"),
    [
        ('{ git = "https://example.com/foo.git", branch = "main" }', 1),
        ('{ git = "https://example.com/foo.git", tag = "v1" }', 1),
        ('{ git = "https://example.com/foo.git", rev = "abc123" }', 1),
        (f'{{ git = "https://example.com/foo.git", rev = "{SHA}" }}', 0),
        ('{ path = "../foo" }', 0),
    ],
)
def test_uv_git_sources(tmp_path: Path, source: str, code: int):
    root = repo(tmp_path, ["foo==1.0.0"])
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    write(root / "pyproject.toml", text + f"\n[tool.uv.sources]\nfoo = {source}\n")
    assert check(root).code == code


@pytest.mark.parametrize(
    ("spec", "code"),
    [
        ("github:owner/repo#main", 1),
        ("git+https://example.com/r.git", 1),
        (f"github:owner/repo#{SHA}", 0),
        (f"git+https://example.com/r.git#{SHA}", 0),
    ],
)
def test_node_git_sources(tmp_path: Path, spec: str, code: int):
    assert check(node_repo(tmp_path, {"r": spec})).code == code


# --------------------------------------------------------------------------- real repo


def test_real_repo_passes():
    result = pins.run(Context(root=DEFAULT_ROOT))
    assert result.code == 0, result.violations


def test_empty_repo_passes(tmp_path: Path):
    assert check(tmp_path).code == 0


# --------------------------------------------------------------------------- CI gate shape


def ci_jobs(text: str) -> dict[str, list[str]]:
    """Top-level jobs of the workflow, each with its own lines (a shape reader, not YAML)."""
    jobs: dict[str, list[str]] = {}
    lines = text.splitlines()
    start = lines.index("jobs:")
    current: str | None = None
    for line in lines[start + 1 :]:
        if line and not line.startswith(" "):
            break
        m = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if m:
            current = str(m.group(1))
            jobs[current] = []
        elif current is not None:
            jobs[current].append(line)
    return jobs


def gate_needs(lines: list[str]) -> list[str]:
    i = lines.index("    needs:")
    out = []
    for line in lines[i + 1 :]:
        m = re.match(r"^      - ([A-Za-z0-9_-]+)\s*$", line)
        if not m:
            break
        out.append(m.group(1))
    return out


def gate_script(lines: list[str]) -> str:
    i = next(n for n, line in enumerate(lines) if line.strip() == "run: |")
    body = []
    for line in lines[i + 1 :]:
        if line.strip() and not line.startswith(" " * 10):
            break
        body.append(line[10:])
    return "\n".join(body) + "\n"


def test_ci_gate_needs_every_job():
    jobs = ci_jobs(CI.read_text(encoding="utf-8"))
    assert {"checks", "py314", "ts", "gate"} <= set(jobs)
    gate = jobs["gate"]
    assert sorted(gate_needs(gate)) == sorted(j for j in jobs if j != "gate")
    assert "    if: always()" in gate


def test_ci_has_314_lane_and_base_policy_step():
    text = CI.read_text(encoding="utf-8")
    jobs = ci_jobs(text)
    assert any("--python 3.14 pytest" in line for line in jobs["py314"])
    checks = "\n".join(jobs["checks"])
    policy = checks.index("tooling.checks.policy --base")
    assert "github.event.pull_request.base.sha" in checks
    assert policy < checks.index("python -m tooling.checks --require-ref")


@pytest.mark.skipif(not (shutil.which("jq") and shutil.which("bash")), reason="needs bash and jq")
@pytest.mark.parametrize(
    ("results", "code"),
    [
        (["success", "success", "success"], 0),
        (["success", "skipped", "success"], 1),
        (["success", "success", "cancelled"], 1),
        (["failure", "success", "success"], 1),
    ],
)
def test_ci_gate_fails_on_anything_but_success(results: list[str], code: int):
    script = gate_script(ci_jobs(CI.read_text(encoding="utf-8"))["gate"])
    needs = {
        name: {"result": r, "outputs": {}} for name, r in zip(["a", "b", "c"], results, strict=True)
    }
    out = subprocess.run(
        ["bash", "-e", "-c", script],
        env={"NEEDS": json.dumps(needs), "PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == code, out.stdout + out.stderr


def test_ci_ts_job_steps_in_order():
    ts = "\n".join(ci_jobs(CI.read_text(encoding="utf-8"))["ts"])
    steps = [
        "node-version-file: .nvmrc",
        "run: corepack enable",
        "run: pnpm install --frozen-lockfile",
        "run: pnpm lint",
        "run: pnpm fmt:check",
        "run: pnpm typecheck",
        "run: pnpm test",
    ]
    positions = [ts.index(step) for step in steps]
    assert positions == sorted(positions)
