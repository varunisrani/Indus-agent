"""P24-40: ``indus vault`` add / list / remove / lock / unlock (hidden prompts, never argv)."""

from __future__ import annotations

import pytest

from indus.cli.commands import vault as vault_cmd
from indus.cli.commands._support import secret_store
from indus.foundation.scope import Scope
from indus.tools.builtin.browser.vault import KEY_NAME, LocalVault
from tests.unit.cli.commands.cmdrun import Ran, run_cli

PASSWORD = "cli-" + "secret-" + "pass-" + "word"
CARD = "5555" + "4444" + "3333" + "1111"


def _run(capsys: pytest.CaptureFixture[str], scope: Scope, *args: str, tty: bool = False) -> Ran:
    return run_cli(capsys, scope, "vault", *args, tty=tty)


class Prompts:
    """Scripted prompt answers; records (question, answer, hidden)."""

    def __init__(self) -> None:
        self.queue: list[str] = []
        self.seen: list[tuple[str, str, bool]] = []

    def __call__(self, text: str, *, hidden: bool, required: bool = True) -> str:
        value = self.queue.pop(0)
        self.seen.append((text, value, hidden))
        return value


@pytest.fixture
def prompts(monkeypatch: pytest.MonkeyPatch) -> Prompts:
    made = Prompts()
    monkeypatch.setattr(vault_cmd, "_ask", made)
    return made


def test_add_needs_a_terminal(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    ran = _run(capsys, scope, "add", "--kind", "login")
    assert ran.code == 1 and "hidden prompts" in ran.err
    assert not (scope.home / "vault").exists()


def test_add_login_reads_secrets_hidden_and_stores_encrypted(
    capsys: pytest.CaptureFixture[str], scope: Scope, prompts: Prompts
) -> None:
    prompts.queue.extend(["me@example.com", PASSWORD, ""])
    ran = _run(
        capsys,
        scope,
        "add",
        "--kind",
        "login",
        "--origin",
        "https://github.com/login",
        "--label",
        "GitHub",
        tty=True,
    )
    assert ran.code == 0, ran.err
    assert "Saved login local-" in ran.out and "https://github.com" in ran.out
    assert PASSWORD not in ran.out + ran.err
    hidden = {text: flag for text, _, flag in prompts.seen}
    assert hidden["Password (hidden)"] is True and hidden["Username or email"] is False
    raw = (scope.home / "vault" / "local.vault").read_bytes()
    assert PASSWORD.encode() not in raw
    assert secret_store(scope).get(KEY_NAME)  # the key lives in the secret store
    [meta] = LocalVault().list_items(scope.with_secrets(secret_store(scope)))
    assert meta.origins == ("https://github.com",) and meta.identifier == "me@example.com"


def test_add_payment_reads_card_fields_hidden(
    capsys: pytest.CaptureFixture[str], scope: Scope, prompts: Prompts
) -> None:
    prompts.queue.extend(["", "Me", CARD, "11/29", "123"])
    ran = _run(
        capsys, scope, "add", "--kind", "payment", "--origin", "https://shop.example", tty=True
    )
    assert ran.code == 0, ran.err
    assert all(flag for text, _, flag in prompts.seen if text != "Label")
    assert CARD not in ran.out


def test_list_shows_metadata_never_values(
    capsys: pytest.CaptureFixture[str], scope: Scope, prompts: Prompts
) -> None:
    empty = _run(capsys, scope)
    assert empty.code == 0 and "empty" in empty.out
    prompts.queue.extend(["", "me@example.com", PASSWORD, ""])
    _run(capsys, scope, "add", "--kind", "login", "--origin", "https://x.example", tty=True)
    listed = _run(capsys, scope, "list")
    assert listed.code == 0
    assert "me@example.com" in listed.out and "https://x.example" in listed.out
    assert PASSWORD not in listed.out


def test_remove_and_unknown_handle(
    capsys: pytest.CaptureFixture[str], scope: Scope, prompts: Prompts
) -> None:
    prompts.queue.extend(["", "me@example.com", PASSWORD, ""])
    _run(capsys, scope, "add", "--kind", "login", "--origin", "https://x.example", tty=True)
    [meta] = LocalVault().list_items(scope.with_secrets(secret_store(scope)))
    assert _run(capsys, scope, "remove", "local-000000000000").code == 1
    removed = _run(capsys, scope, "rm", meta.handle)
    assert removed.code == 0 and meta.handle in removed.out
    assert LocalVault().list_items(scope.with_secrets(secret_store(scope))) == []
    assert _run(capsys, scope, "remove").code == 2


def test_lock_and_unlock(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    locked = _run(capsys, scope, "lock")
    assert locked.code == 0 and LocalVault().locked(scope)
    assert "locked" in _run(capsys, scope, "list").out
    unlocked = _run(capsys, scope, "unlock")
    assert unlocked.code == 0 and "unlocked" in unlocked.out
    assert not LocalVault().locked(scope)
    again = _run(capsys, scope, "unlock")
    assert "was not locked" in again.out


def test_bad_origin_is_refused(
    capsys: pytest.CaptureFixture[str], scope: Scope, prompts: Prompts
) -> None:
    prompts.queue.extend(["", "me", PASSWORD, ""])
    ran = _run(capsys, scope, "add", "--kind", "login", "--origin", "ftp://x", tty=True)
    assert ran.code == 1 and "http(s)" in ran.err
