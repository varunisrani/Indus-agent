"""P25-21: ``scripts/release/release_gates.sh`` — every gate on a fresh clone, one line each.

A tiny local repository stands in for the candidate and the gates are fake commands (``true``,
``exit 3``, ``git rev-parse``); the real gates in ``scripts/release/gates.txt`` are only parsed,
never run here.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.platforms("posix")

REPO_ROOT = Path(__file__).resolve().parents[3]
RUNNER = REPO_ROOT / "scripts" / "release" / "release_gates.sh"
REAL_GATES = REPO_ROOT / "scripts" / "release" / "gates.txt"
LINE = re.compile(r"^(?P<name>[a-z0-9][a-z0-9_-]*) exit=(?P<code>\d+) (?P<secs>\d+)s$")

# Every gate P25-21 names (docs/plan/phases/P25-hardening-parity-release.md), by our gate name.
PLANNED_GATES = {
    "install",
    "lint",
    "format",
    "types",
    "unit",
    "contract",
    "integration",
    "webe2e",
    "tuie2e",
    "desktope2e",
    "cleanroom",
    "acceptance",
    "traps",
    "acpdrift",
    "mcpdrift",
    "bundle",
    "perf",
    "docs",
    "notices",
    "audit",
    "parity",
    "similarity",
    "installers",
    "dockerimage",
    "nix",
}


def git_env(home: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "Release Test",
        "GIT_AUTHOR_EMAIL": "release-test@" + "example.invalid",
        "GIT_COMMITTER_NAME": "Release Test",
        "GIT_COMMITTER_EMAIL": "release-test@" + "example.invalid",
        "LC_ALL": "C",
    }


class Candidate:
    """A source repository with two commits and an untracked file beside them."""

    def __init__(self, tmp: Path) -> None:
        self.home = tmp / "git-home"
        self.home.mkdir()
        self.env = git_env(self.home)
        self.path = tmp / "source"
        self.path.mkdir()
        self.git("init", "--quiet", "--initial-branch=main")
        (self.path / "README.txt").write_text("first\n", encoding="utf-8")
        self.git("add", "README.txt")
        self.git("commit", "--quiet", "-m", "first")
        self.first = self.git("rev-parse", "HEAD")
        (self.path / "README.txt").write_text("second\n", encoding="utf-8")
        self.git("commit", "--quiet", "-am", "second")
        self.second = self.git("rev-parse", "HEAD")
        (self.path / "untracked.txt").write_text("only in the working copy\n", encoding="utf-8")

    def git(self, *args: str) -> str:
        done = subprocess.run(
            ["git", *args], cwd=self.path, env=self.env,
            capture_output=True, text=True, check=True, timeout=60,
        )  # fmt: skip
        return done.stdout.strip()


@pytest.fixture
def candidate(tmp_path: Path) -> Candidate:
    return Candidate(tmp_path)


def write_gates(tmp: Path, text: str) -> Path:
    path = tmp / "gates.txt"
    path.write_text(text, encoding="utf-8")
    return path


def run_gates(
    candidate: Candidate, sha: str, gates: Path, work: Path
) -> subprocess.CompletedProcess[str]:
    env = {**candidate.env, "WORK_DIR": str(work)}
    return subprocess.run(
        ["bash", str(RUNNER), str(candidate.path), sha, str(gates)],
        env=env, capture_output=True, text=True, timeout=120, check=False,
        stdin=subprocess.DEVNULL,
    )  # fmt: skip


def summary(work: Path) -> list[tuple[str, int]]:
    rows = []
    for line in (work / "gates.txt").read_text(encoding="utf-8").splitlines():
        m = LINE.match(line)
        assert m, line
        rows.append((m.group("name"), int(m.group("code"))))
    return rows


def test_every_gate_runs_after_a_failure(candidate: Candidate, tmp_path: Path) -> None:
    gates = write_gates(
        tmp_path,
        "first true\nbroken exit 3\nmissing no-such-command-anywhere\nlast echo still here\n",
    )
    work = tmp_path / "work"
    done = run_gates(candidate, candidate.second, gates, work)
    assert done.returncode == 1, done.stderr
    assert summary(work) == [("first", 0), ("broken", 3), ("missing", 127), ("last", 0)]
    assert "still here" in (work / "logs" / "last.log").read_text(encoding="utf-8")
    assert "2 of 4 gates failed" in done.stdout


def test_summary_and_logs(candidate: Candidate, tmp_path: Path) -> None:
    gates = write_gates(
        tmp_path,
        "# a comment line\n\n"
        "  hello   echo out; echo err >&2\n"
        'where echo "$GATE $(basename "$LOG_DIR") $RELEASE_SHA"\n'
        'stdin_closed test -z "$(cat)"\n',
    )
    work = tmp_path / "work"
    done = run_gates(candidate, candidate.second, gates, work)
    assert done.returncode == 0, done.stderr
    names = [name for name, _ in summary(work)]
    assert names == ["hello", "where", "stdin_closed"]
    assert [line for line in done.stdout.splitlines() if LINE.match(line)] == (
        (work / "gates.txt").read_text(encoding="utf-8").splitlines()
    )
    hello = (work / "logs" / "hello.log").read_text(encoding="utf-8")
    assert "out" in hello and "err" in hello
    where = (work / "logs" / "where.log").read_text(encoding="utf-8").strip()
    assert where == f"where logs {candidate.second}"
    run = (work / "run.txt").read_text(encoding="utf-8")
    assert f"commit {candidate.second}" in run and "failed 0" in run


def test_clone_is_fresh_and_at_the_commit(candidate: Candidate, tmp_path: Path) -> None:
    gates = write_gates(
        tmp_path,
        "head git rev-parse HEAD\n"
        "readme cat README.txt\n"
        "no_untracked test ! -e untracked.txt\n"
        'detached test -z "$(git branch --show-current)"\n',
    )
    work = tmp_path / "work"
    done = run_gates(candidate, candidate.first[:10], gates, work)
    assert done.returncode == 0, (done.stdout, done.stderr)
    logs = work / "logs"
    assert (logs / "head.log").read_text(encoding="utf-8").strip() == candidate.first
    assert (logs / "readme.log").read_text(encoding="utf-8").strip() == "first"
    assert summary(work) == [("head", 0), ("readme", 0), ("no_untracked", 0), ("detached", 0)]
    # The source checkout is untouched.
    assert candidate.git("rev-parse", "HEAD") == candidate.second


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("same true\nsame true\n", "duplicate gate: same"),
        ("Bad-Name true\n", "bad gate name"),
        ("lonely\n", "has no command"),
        ("# only a comment\n\n", "no gates"),
    ],
)
def test_a_bad_gates_file_is_refused_before_cloning(
    candidate: Candidate, tmp_path: Path, text: str, message: str
) -> None:
    gates = write_gates(tmp_path, text)
    work = tmp_path / "work"
    done = run_gates(candidate, candidate.second, gates, work)
    assert done.returncode == 2
    assert message in done.stderr
    assert not (work / "clone").exists()


def test_an_unknown_commit_is_a_setup_error(candidate: Candidate, tmp_path: Path) -> None:
    gates = write_gates(tmp_path, "never touch ran\n")
    work = tmp_path / "work"
    done = run_gates(candidate, "0123456789abcdef0123456789abcdef01234567", gates, work)
    assert done.returncode == 2
    assert "is not in" in done.stderr
    assert not (work / "gates.txt").exists()
    assert not (work / "clone" / "ran").exists()


def test_a_work_folder_with_files_is_refused(candidate: Candidate, tmp_path: Path) -> None:
    gates = write_gates(tmp_path, "ok true\n")
    work = tmp_path / "work"
    work.mkdir()
    (work / "old-run.txt").write_text("left over\n", encoding="utf-8")
    done = run_gates(candidate, candidate.second, gates, work)
    assert done.returncode == 2
    assert "not empty" in done.stderr


def test_arguments_are_checked(candidate: Candidate, tmp_path: Path) -> None:
    gates = write_gates(tmp_path, "ok true\n")
    env = {**candidate.env, "WORK_DIR": str(tmp_path / "work")}
    few = subprocess.run(
        ["bash", str(RUNNER), str(candidate.path)], env=env,
        capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip
    assert few.returncode == 2 and "usage:" in few.stderr
    done = run_gates(candidate, "main; rm -rf /", gates, tmp_path / "work")
    assert done.returncode == 2 and "not a commit id" in done.stderr


def parse_gates(path: Path) -> list[tuple[str, str]]:
    """The same reading rules as the runner (kept small; the runner is the authority)."""
    gates = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, _, command = line.partition(" ")
        gates.append((name, command.strip()))
    return gates


def test_the_real_gates_file_names_every_planned_gate() -> None:
    gates = parse_gates(REAL_GATES)
    names = [name for name, _ in gates]
    assert len(names) == len(set(names))
    assert all(re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name) for name in names)
    assert all(command for _, command in gates)
    assert set(names) >= PLANNED_GATES, PLANNED_GATES - set(names)
    assert names[0] == "install"
    commands = dict(gates)
    assert "--frozen-lockfile" in commands["install"] and "--locked" in commands["install"]
    for gate in ("cleanroom", "acceptance", "traps", "similarity"):
        assert "--require-ref" in commands[gate], gate
    assert "--strict" in commands["acceptance"] and "--strict" in commands["traps"]
    assert "--closest 20" in commands["similarity"]
    assert "--all" in commands["parity"] and "--ledger" in commands["parity"]


def test_the_real_gates_file_runs_the_security_audit_and_release_lane_suites() -> None:
    """Gates that arrived after the plan's list (D-16.961): wave 43's security suite and
    dependency audit (online, from the fresh clone) and wave 44's release-lane rehearsal."""
    commands = dict(parse_gates(REAL_GATES))
    assert "tests/security" in commands["security"]
    assert "--ignore=tests/security" in commands["unit"]
    assert "tooling.checks.deps_audit" in commands["audit"] and "--refresh" in commands["audit"]
    assert commands["upgrade"].startswith("INDUS_RELEASE_LANE=1 ")
    assert "tests/e2e/upgrade" in commands["upgrade"]


def test_the_real_gates_file_passes_the_runners_own_reading(
    candidate: Candidate, tmp_path: Path
) -> None:
    """The runner refuses a malformed file before cloning; prove the real file is not refused by
    giving it a commit that does not exist (setup stops at the commit, after the file was read)."""
    done = run_gates(candidate, "0" * 40, REAL_GATES, tmp_path / "work")
    assert done.returncode == 2
    assert "is not in" in done.stderr, done.stderr
