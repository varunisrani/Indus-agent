"""``@`` context references (P08-19, A8)."""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Coroutine, Iterator
from pathlib import Path
from typing import Any

import pytest

from indus.core import references
from indus.core.references import (
    MAX_FILE_BYTES,
    MAX_FOLDER_ENTRIES,
    SECTION_OPEN,
    Expansion,
    ReferenceContext,
    ReferenceRefused,
    budget_for_trigger,
    budget_for_window,
    expand,
    find_references,
    register_prefix,
    unregister_prefix,
)
from indus.foundation.scope import Scope
from indus.platform.locate import which


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


@pytest.fixture
def work(tmp_path: Path) -> Path:
    root = tmp_path / "work"
    root.mkdir()
    (root / "a.py").write_text("one\ntwo\nthree\nfour\nfive\n", encoding="utf-8")
    return root


@pytest.fixture
def home_scope(scope: Scope, tmp_path: Path) -> Scope:
    """The scope with HOME pointing at a throwaway home holding credential files."""
    home = tmp_path / "userhome"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "config").write_text("Host x\n", encoding="utf-8")
    (home / ".aws").mkdir()
    (home / ".aws" / "credentials").write_text("[default]\n", encoding="utf-8")
    (home / ".netrc").write_text("machine x\n", encoding="utf-8")
    return scope.with_env(scope.env.with_overlay({"HOME": str(home)}))


def section(expansion: Expansion) -> str:
    return expansion.text.split(SECTION_OPEN, 1)[1]


def test_no_token_means_no_change(scope: Scope, work: Path) -> None:
    for text in ("plain text", "mail me at someone@example.com", "@alice what do you think?"):
        result = run(expand(text, scope, work))
        assert result.text == text and not result.changed


def test_find_references_forms_and_punctuation() -> None:
    text = (
        "look at @file:a.py:2-4, then @folder:src. Also @diff and @git:HEAD~1! "
        '@file:"my notes.txt" (and @url:https://example.com/x?y=1) @file:a.py:2-4 again'
    )
    assert find_references(text) == [
        ("@file:a.py:2-4", "file", "a.py:2-4"),
        ("@folder:src", "folder", "src"),
        ("@diff", "diff", ""),
        ("@git:HEAD~1", "git", "HEAD~1"),
        ("@file:my notes.txt", "file", "my notes.txt"),
        ("@url:https://example.com/x?y=1", "url", "https://example.com/x?y=1"),
    ]


def test_file_line_range_inserts_exactly_those_lines(scope: Scope, work: Path) -> None:
    result = run(expand("explain @file:a.py:2-4 please", scope, work))
    assert result.text.startswith("explain @file:a.py:2-4 please\n\n")
    (block,) = result.blocks
    assert block.body == "two\nthree\nfour\n"
    assert block.title == "a.py, lines 2-4"
    assert "one" not in section(result) and "five" not in section(result)


def test_whole_file_and_single_line(scope: Scope, work: Path) -> None:
    whole = run(expand("@file:a.py", scope, work))
    assert whole.blocks[0].body == "one\ntwo\nthree\nfour\nfive\n"
    single = run(expand("@file:a.py:5", scope, work))
    assert single.blocks[0].body == "five\n"


def test_large_file_is_truncated_with_a_marker(scope: Scope, work: Path) -> None:
    (work / "big.log").write_text("x" * (MAX_FILE_BYTES + 5000), encoding="utf-8")
    result = run(expand("@file:big.log", scope, work))
    body = result.blocks[0].body
    assert body.startswith("x" * 100) and "[... truncated" in body
    assert len(body) < MAX_FILE_BYTES + 300


def test_credential_files_refused_with_a_note(home_scope: Scope, work: Path) -> None:
    (work / ".env").write_text("TOKEN=abc\n", encoding="utf-8")
    (work / "server.pem").write_text("-----BEGIN-----\n", encoding="utf-8")
    (work / "id_ed25519").write_text("key\n", encoding="utf-8")
    (work / ".env.example").write_text("TOKEN=\n", encoding="utf-8")
    text = (
        "@file:.env @file:~/.ssh/config @file:~/.aws/credentials @file:~/.netrc "
        "@file:server.pem @file:id_ed25519 @folder:~/.ssh @file:.env.example"
    )
    result = run(expand(text, home_scope, work))
    refused = {r.token for r in result.refusals}
    assert refused == {
        "@file:.env",
        "@file:~/.ssh/config",
        "@file:~/.aws/credentials",
        "@file:~/.netrc",
        "@file:server.pem",
        "@file:id_ed25519",
        "@folder:~/.ssh",
    }
    assert [b.token for b in result.blocks] == ["@file:.env.example"]
    body = section(result)
    assert "TOKEN=abc" not in body and "[@file:.env was not included:" in body


def test_indus_auth_store_refused(scope: Scope, work: Path) -> None:
    (scope.home / "auth.json").write_text("{}", encoding="utf-8")
    result = run(expand(f"@file:{scope.home / 'auth.json'}", scope, work))
    assert result.refusals and not result.blocks


def test_symlink_to_a_credential_is_refused(home_scope: Scope, work: Path) -> None:
    target = Path(home_scope.env["HOME"]) / ".aws" / "credentials"
    (work / "innocent.txt").symlink_to(target)
    result = run(expand("@file:innocent.txt", home_scope, work))
    assert result.refusals and not result.blocks


def test_file_problems_are_refusals(scope: Scope, work: Path) -> None:
    (work / "pic.png").write_bytes(b"\x89PNG\r\n")
    (work / "blob.dat").write_bytes(b"abc\x00def")
    text = "@file:missing.txt @file:pic.png @file:blob.dat @file:a.py:9-12 @file:a.py:4-2 @file:."
    result = run(expand(text, scope, work))
    assert len(result.refusals) == 6 and not result.blocks


def test_folder_caps_depth_and_count(scope: Scope, work: Path) -> None:
    deep = work / "pkg" / "sub" / "deeper"
    deep.mkdir(parents=True)
    (deep / "hidden_from_listing.txt").write_text("x", encoding="utf-8")
    (work / "pkg" / "sub" / "visible.txt").write_text("x", encoding="utf-8")
    (work / "node_modules").mkdir()
    many = work / "many"
    many.mkdir()
    for n in range(MAX_FOLDER_ENTRIES + 30):
        (many / f"f{n:03}.txt").write_text("", encoding="utf-8")
    listing = run(expand("@folder:pkg", scope, work)).blocks[0].body
    assert "sub/" in listing and "visible.txt" in listing and "hidden_from_listing" not in listing
    root = run(expand("@folder:.", scope, work)).blocks[0].body
    assert "node_modules" not in root
    capped = run(expand("@folder:many", scope, work)).blocks[0].body.splitlines()
    assert (
        len(capped) == MAX_FOLDER_ENTRIES + 1 and capped[-1] == "[... 30 more entries not listed]"
    )


@pytest.fixture
def repo(work: Path, scope: Scope) -> Iterator[Path]:
    if which("git") is None:
        pytest.skip("git is not installed")

    def git(*args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=work,
            check=True,
            capture_output=True,
            env={
                "PATH": scope.env.get("PATH", ""),
                "HOME": str(work),
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@example.invalid",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@example.invalid",
            },
        )

    git("init", "-q")
    git("add", "a.py")
    git("commit", "-q", "-m", "first commit")
    (work / "a.py").write_text("one\nTWO\nthree\nfour\nfive\n", encoding="utf-8")
    yield work


def test_diff_and_commit(scope: Scope, repo: Path) -> None:
    result = run(expand("what changed? @diff and @git:HEAD", scope, repo))
    diff, commit = result.blocks
    assert "-two" in diff.body and "+TWO" in diff.body
    assert "first commit" in commit.body and "+one" in commit.body


def test_bad_refs_and_no_repo_refused(scope: Scope, work: Path, repo: Path) -> None:
    result = run(expand("@git:--output=/tmp/x @git:HEAD..HEAD~1 @git:nosuchref", scope, repo))
    assert len(result.refusals) == 3 and not result.blocks
    outside = work.parent / "elsewhere"
    outside.mkdir()
    assert run(expand("@diff", scope, outside)).refusals


def test_url_to_a_private_address_refused_by_policy(scope: Scope, work: Path) -> None:
    async def never(url: str) -> str:
        raise AssertionError("must not fetch")

    result = run(
        expand(
            "@url:http://10.1.2.3/admin @url:http://169.254.169.254/", scope, work, extract=never
        )
    )
    assert len(result.refusals) == 2
    assert all("URL policy" in r.reason for r in result.refusals)


def test_url_public_goes_through_extract(scope: Scope, work: Path) -> None:
    fetched: list[str] = []

    async def page(url: str) -> str:
        fetched.append(url)
        return "Example page text"

    def resolver(host: str, port: int | None) -> list[str]:
        return ["93.184.215.14"]

    result = run(
        expand("@url:https://example.com/doc", scope, work, extract=page, resolver=resolver)
    )
    assert fetched == ["https://example.com/doc"]
    assert result.blocks[0].body == "Example page text"


def test_over_budget_item_becomes_a_pointer(scope: Scope, work: Path) -> None:
    (work / "big.txt").write_text("y" * 5000, encoding="utf-8")
    result = run(expand("@file:a.py @file:big.txt", scope, work, budget_chars=1000))
    small, big = result.blocks
    assert not small.pointer and big.pointer
    assert "y" * 100 not in result.text
    assert "does not fit the context budget" in big.body
    assert budget_for_window(200_000) == 200_000 and budget_for_window(None) is None


def test_plugin_prefix_expands_through_the_seam(scope: Scope, work: Path) -> None:
    async def ticket(arg: str, ctx: ReferenceContext) -> str:
        if arg == "secret":
            raise ReferenceRefused("that ticket is private")
        return f"Ticket {arg}: login fails on Safari"

    register_prefix("ticket", ticket)
    try:
        result = run(expand("see @ticket:ABC-12 and @ticket:secret", scope, work))
    finally:
        unregister_prefix("ticket")
    assert result.blocks[0].body == "Ticket ABC-12: login fails on Safari"
    assert result.refusals[0].reason == "that ticket is private"
    assert run(expand("@ticket:ABC-12", scope, work)).text == "@ticket:ABC-12"
    with pytest.raises(ValueError):
        register_prefix("file", ticket)


def test_module_exports() -> None:
    assert {"file", "folder", "diff", "git", "url"} == references.BUILTIN_KINDS


def test_budget_from_the_compression_trigger() -> None:
    """P08-02 joined: the room under the trigger after the last reported prompt."""
    assert budget_for_trigger(100_000, 60_000) == 160_000
    assert budget_for_trigger(100_000, None) == 400_000
    assert budget_for_trigger(100_000, 150_000) == 0
    assert budget_for_trigger(0, 10) is None
