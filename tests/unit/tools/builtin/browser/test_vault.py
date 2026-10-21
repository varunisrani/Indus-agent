"""``browser_vault`` (P24-40): model-blind fills, origin checked twice, approvals, no leaks."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.scope import Scope
from indus.protocol.envelope import ErrorObject
from indus.protocol.requests_port import AnswerModel, RequestUnsupported
from indus.protocol.server_requests import (
    ApprovalAnswer,
    ApprovalChoice,
    ClarifyAnswer,
    SecretAnswer,
)
from indus.tools.builtin.browser.vault import (
    FILL_JS,
    INSPECT_JS,
    Bitwarden,
    CliResult,
    LocalVault,
    OnePassword,
    VaultTool,
    classify,
    normalize_origin,
    register_vault_tool,
    totp_now,
)
from indus.tools.registry import ToolRegistry
from tests.fakes.agent import DictConfig
from tests.fakes.browser import FakePage, FakeSite, el
from tests.fakes.secret_store import MemorySecretStore
from tests.unit.tools.builtin.browser._rig import Rig, make_rig, make_scope_for

PASSWORD = "correct-" + "horse-" + "battery-" + "staple"
CARD = "4111" + "1111" + "1111" + "1111"
CVC = "9" + "31"
MASTER = "master-" + "pass-" + "phrase-" + "42"
SEED = "JBSWY3DPEHPK3PXP"  # the RFC 6238 style demo seed (not a credential)

BANK = "https://bank.example/login"
EVIL = "https://bank.example.evil/login"
SHOP = "https://shop.example/checkout"
LOGIN_FORM = [
    {"type": "email", "name": "user", "id": "user", "autocomplete": "username"},
    {"type": "password", "name": "pass", "id": "pass", "autocomplete": "current-password"},
]
CARD_FORM = [
    {"type": "text", "name": "cc", "autocomplete": "cc-number"},
    {"type": "text", "name": "exp", "autocomplete": "cc-exp"},
    {"type": "text", "name": "cvc", "autocomplete": "cc-csc"},
]
OTP_FORM = [{"type": "text", "name": "otp", "autocomplete": "one-time-code"}]


class Form:
    """A scripted page: field inspection and fills per URL."""

    def __init__(self, controls: Mapping[str, list[dict[str, Any]]]) -> None:
        self.controls = dict(controls)
        self.filled: dict[str, str] = {}
        self.move_to: str | None = None
        self.fail_fill = False
        self.scripts: list[str] = []

    async def hook(self, page: FakePage, expression: str) -> Any:
        self.scripts.append(expression)
        if expression.startswith(f"({INSPECT_JS})"):
            nonce = json.loads(expression[len(INSPECT_JS) + 3 : -1])
            rows = [
                {**c, "stamp": f"{nonce}-{i}", "placeholder": "", "label": "", "maxlength": 0}
                for i, c in enumerate(self.controls.get(page.current, []))
            ]
            if self.move_to is not None:
                page.current = self.move_to  # the page moves on after the pre-check
            return rows
        if expression.startswith(f"({FILL_JS})"):
            if self.fail_fill:
                raise RuntimeError(f"Evaluation failed: {expression}")
            args = json.loads(expression[len(FILL_JS) + 3 : -1])
            origin = normalize_origin(page.current)
            if origin != args["expected"]:
                return {"refused": "origin_changed", "found": origin}
            for stamp, value in args["fills"]:
                self.filled[stamp] = value
            return {"filled": len(args["fills"])}
        raise AssertionError(f"unexpected script {expression[:40]}")


class Broker:
    """Scripted answers for approval, secret and clarify requests."""

    def __init__(
        self,
        approvals: Sequence[str] = (),
        secrets: Sequence[str | None] = (),
        texts: Sequence[str] = (),
        unsupported: bool = False,
    ) -> None:
        self.approvals = list(approvals)
        self.secrets = list(secrets)
        self.texts = list(texts)
        self.unsupported = unsupported
        self.asked: list[tuple[str, dict[str, Any]]] = []

    async def ask(
        self,
        kind: str,
        payload: Mapping[str, Any],
        *,
        timeout: float | None = None,
        turn_id: str | None = None,
    ) -> AnswerModel:
        self.asked.append((kind, dict(payload)))
        if self.unsupported:
            raise RequestUnsupported(kind, ErrorObject(code=-32000, message="no surface"))
        if kind == "approval":
            return ApprovalAnswer(choice=ApprovalChoice(self.approvals.pop(0)))
        if kind == "secret":
            return SecretAnswer(value=self.secrets.pop(0))
        if kind == "clarify":
            key = payload["questions"][0]["id"]
            return ClarifyAnswer(answers={key: [self.texts.pop(0)]})
        raise AssertionError(kind)

    def cancel_all(self, reason: Any) -> int:
        return 0

    def human_wait_seconds(self) -> float:
        return 0.0


def setup(
    tmp_path: Path, url: str = BANK, controls: Mapping[str, list[dict[str, Any]]] | None = None
) -> tuple[Rig, Form, VaultTool, Scope]:
    sites = {
        BANK: FakeSite(title="Bank", elements=[el("textbox", "Password", value=PASSWORD)]),
        EVIL: FakeSite(title="Evil"),
        SHOP: FakeSite(title="Shop"),
        "https://bank.example/2fa": FakeSite(title="Code"),
    }
    rig = make_rig(sites)
    rig.resolver.set("bank.example", ["93.184.216.40"])
    rig.resolver.set("bank.example.evil", ["93.184.216.41"])
    form = Form(controls or {BANK: LOGIN_FORM, EVIL: LOGIN_FORM, SHOP: CARD_FORM})
    rig.driver.eval_hook = form.hook
    tool = VaultTool(rig.manager)
    scope = make_scope_for(tmp_path, rig.clock, {"browser": {"vault_sources": ["local"]}})
    scope = scope.with_secrets(MemorySecretStore())
    return rig, form, tool, scope


def go_to(rig: Rig, scope: Scope, url: str) -> dict[str, Any]:
    return json.loads(asyncio.run(rig.tools.pages.navigate({"url": url}, scope=scope)))


def call(
    tool: VaultTool, scope: Scope, broker: Broker | None = None, **args: Any
) -> dict[str, Any]:
    return json.loads(asyncio.run(tool(args, scope=scope, broker=broker)))


def save_login(scope: Scope, origin: str = "https://bank.example") -> str:
    meta = LocalVault().add(
        scope,
        kind="login",
        label="Bank",
        origin=origin,
        secret={"password": PASSWORD},
        identifier="me@example.com",
        otp_seed=SEED,
    )
    return meta.handle


def save_card(scope: Scope) -> str:
    meta = LocalVault().add(
        scope,
        kind="payment",
        label="Visa",
        origin="https://shop.example",
        secret={"cc_number": CARD, "cc_exp": "12/30", "cc_csc": CVC, "cc_name": "Me"},
    )
    return meta.handle


def test_fill_writes_the_password_on_the_exact_origin(tmp_path: Path) -> None:
    rig, form, tool, scope = setup(tmp_path)
    handle = save_login(scope)
    go_to(rig, scope, BANK)
    out = call(tool, scope, action="fill", handle=handle)
    assert out["success"] is True and out["filled_fields"] == 1
    assert out["origin"] == "https://bank.example" and out["kind"] == "login"
    assert list(form.filled.values()) == [PASSWORD]


def test_origin_mismatch_refuses_fill(tmp_path: Path) -> None:
    rig, form, tool, scope = setup(tmp_path)
    handle = save_login(scope)
    go_to(rig, scope, EVIL)
    out = call(tool, scope, action="fill", handle=handle)
    assert out["success"] is False and out["error_type"] == "origin_mismatch"
    assert form.filled == {} and form.scripts == []  # nothing inspected, nothing written


def test_redirect_between_checks_refuses(tmp_path: Path) -> None:
    rig, form, tool, scope = setup(tmp_path)
    handle = save_login(scope)
    go_to(rig, scope, BANK)
    form.move_to = EVIL
    out = call(tool, scope, action="fill", handle=handle)
    assert out["success"] is False and out["error_type"] == "origin_changed"
    assert form.filled == {}
    assert PASSWORD not in json.dumps(out)


def test_payment_fill_without_approval_is_refused(tmp_path: Path) -> None:
    rig, form, tool, scope = setup(tmp_path)
    handle = save_card(scope)
    go_to(rig, scope, SHOP)
    for broker in (None, Broker(approvals=["deny"]), Broker(unsupported=True)):
        out = call(tool, scope, broker, action="fill", handle=handle)
        assert out["success"] is False and out["error_type"] == "payment_declined"
    assert form.filled == {}


def test_payment_fill_with_approval_fills_card_fields(tmp_path: Path) -> None:
    rig, form, tool, scope = setup(tmp_path)
    handle = save_card(scope)
    go_to(rig, scope, SHOP)
    broker = Broker(approvals=["once"])
    out = call(tool, scope, broker, action="fill", handle=handle)
    assert out["success"] is True and out["filled_fields"] == 3
    assert out["fields"] == ["cc_csc", "cc_exp", "cc_number"]
    assert sorted(form.filled.values()) == sorted([CARD, "12/30", CVC])
    [(kind, payload)] = broker.asked
    assert kind == "approval" and "shop.example" in payload["command"]
    assert CARD not in json.dumps(payload)


def test_list_shows_metadata_only_and_locked_managers(tmp_path: Path) -> None:
    rig, _form, tool, scope = setup(tmp_path)
    save_login(scope)
    save_card(scope)
    op = OnePassword(runner=FakeCli({}), locate=lambda s, n: Path("/usr/bin/op"))
    tool.onepassword = op
    scope = scope.with_config(DictConfig({"browser": {"vault_sources": ["local", "onepassword"]}}))
    out = call(tool, scope, Broker(), action="list")
    text = json.dumps(out)
    assert out["success"] is True and len(out["items"]) == 2
    login = next(i for i in out["items"] if i["kind"] == "login")
    assert login["identifier"] == "me@example.com" and login["two_factor"] == "automatic"
    assert login["origin"] == "https://bank.example"
    assert out["locked"] == [
        {
            "backend": "onepassword",
            "display_name": "1Password",
            "unlock": "browser_vault action=unlock",
        }
    ]
    for value in (PASSWORD, CARD, SEED):
        assert value not in text


def test_list_when_empty_hints_save_login(tmp_path: Path) -> None:
    _rig, _form, tool, scope = setup(tmp_path)
    out = call(tool, scope, action="list")
    assert out["items"] == [] and "save_login" in out["hint"]


def test_save_login_asks_hidden_and_fills(tmp_path: Path) -> None:
    rig, form, tool, scope = setup(tmp_path)
    go_to(rig, scope, BANK)
    broker = Broker(texts=["me@example.com"], secrets=[PASSWORD])
    out = call(tool, scope, broker, action="save_login", label="My bank")
    assert out["success"] is True and out["identifier"] == "me@example.com"
    assert out["fill"]["success"] is True
    assert list(form.filled.values()) == [PASSWORD]
    kinds = [k for k, _ in broker.asked]
    assert kinds == ["clarify", "secret"]
    stored = LocalVault().list_items(scope)
    assert [m.origins for m in stored] == [("https://bank.example",)]
    assert PASSWORD not in json.dumps(out)
    raw = (scope.home / "vault" / "local.vault").read_bytes()
    assert PASSWORD.encode() not in raw  # encrypted at rest


def test_save_login_declined(tmp_path: Path) -> None:
    rig, form, tool, scope = setup(tmp_path)
    go_to(rig, scope, BANK)
    out = call(tool, scope, Broker(texts=["me@example.com"], secrets=[None]), action="save_login")
    assert out["error_type"] == "save_declined"
    assert LocalVault().list_items(scope) == []
    assert call(tool, scope, None, action="save_login")["error_type"] == "prompt_unavailable"


def test_enter_code_uses_the_seed(tmp_path: Path) -> None:
    rig, form, tool, scope = setup(tmp_path, controls={"https://bank.example/2fa": OTP_FORM})
    handle = save_login(scope)
    go_to(rig, scope, "https://bank.example/2fa")
    out = call(tool, scope, action="enter_code", handle=handle)
    assert out["success"] is True and out["source"] == "local"
    expected = totp_now(SEED, rig.clock.now().timestamp())
    assert list(form.filled.values()) == [expected]
    assert expected not in json.dumps(out)


def test_enter_code_asks_the_user_without_a_seed(tmp_path: Path) -> None:
    rig, form, tool, scope = setup(tmp_path, controls={"https://bank.example/2fa": OTP_FORM})
    go_to(rig, scope, "https://bank.example/2fa")
    out = call(tool, scope, Broker(secrets=["123 456"]), action="enter_code")
    assert out["success"] is True and out["source"] == "user"
    assert list(form.filled.values()) == ["123456"]
    declined = call(tool, scope, Broker(secrets=[None]), action="enter_code")
    assert declined["error_type"] == "code_declined"


def test_filled_value_is_masked_in_a_later_snapshot(tmp_path: Path) -> None:
    rig, _form, tool, scope = setup(tmp_path)
    handle = save_login(scope)
    before = go_to(rig, scope, BANK)
    assert PASSWORD in before["snapshot"]  # the page echoes it (only for this check)
    call(tool, scope, action="fill", handle=handle)
    after = json.loads(asyncio.run(rig.tools.pages.snapshot({}, scope=scope)))
    assert PASSWORD not in after["snapshot"]


def test_fill_errors_never_quote_the_script(tmp_path: Path) -> None:
    rig, form, tool, scope = setup(tmp_path)
    handle = save_login(scope)
    go_to(rig, scope, BANK)
    form.fail_fill = True
    out = call(tool, scope, action="fill", handle=handle)
    assert out["success"] is False and PASSWORD not in json.dumps(out)


def test_no_secret_in_any_result_or_log(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    rig, form, tool, scope = setup(tmp_path)
    results: list[str] = []
    with caplog.at_level(logging.DEBUG):
        login = save_login(scope)
        card = save_card(scope)
        go_to(rig, scope, BANK)
        for args, broker in (
            ({"action": "list"}, None),
            ({"action": "fill", "handle": login}, None),
            ({"action": "save_login"}, Broker(texts=["x@example.com"], secrets=[PASSWORD])),
            ({"action": "enter_code", "handle": login}, None),
            ({"action": "fill", "handle": card}, Broker(approvals=["once"])),
            ({"action": "unlock", "backend": "local"}, None),
            ({"action": "bogus"}, None),
        ):
            results.append(asyncio.run(tool(args, scope=scope, broker=broker)))
        go_to(rig, scope, EVIL)
        results.append(asyncio.run(tool({"action": "fill", "handle": login}, scope=scope)))
        form.fail_fill = True
        go_to(rig, scope, BANK)
        results.append(asyncio.run(tool({"action": "fill", "handle": login}, scope=scope)))
    blob = "\n".join(results) + caplog.text
    for value in (PASSWORD, CARD, SEED, MASTER):
        assert value not in blob


def test_local_vault_lock_needs_approval_to_unlock(tmp_path: Path) -> None:
    rig, form, tool, scope = setup(tmp_path)
    handle = save_login(scope)
    tool.local.lock(scope)
    go_to(rig, scope, BANK)
    listed = call(tool, scope, None, action="list")
    assert listed["items"] == [] and listed["locked"][0]["backend"] == "local"
    denied = call(tool, scope, Broker(approvals=["deny"]), action="fill", handle=handle)
    assert denied["error_type"] == "unlock_cancelled" and form.filled == {}
    allowed = call(tool, scope, Broker(approvals=["once"]), action="fill", handle=handle)
    assert allowed["success"] is True


def test_unknown_handle_and_no_page(tmp_path: Path) -> None:
    _rig, _form, tool, scope = setup(tmp_path)
    assert "No vault item" in call(tool, scope, action="fill", handle="local-ffffffffffff")["error"]
    handle = save_login(scope)
    assert "No browser page" in call(tool, scope, action="fill", handle=handle)["error"]


# -- password-manager CLIs --------------------------------------------------------------------


class FakeCli:
    def __init__(self, answers: Mapping[str, CliResult]) -> None:
        self.answers = dict(answers)
        self.calls: list[tuple[list[str], dict[str, str], str | None]] = []

    async def run(
        self,
        argv: Sequence[str],
        env: Mapping[str, str],
        *,
        stdin: str | None = None,
        timeout_s: float = 30.0,
    ) -> CliResult:
        self.calls.append((list(argv), dict(env), stdin))
        key = " ".join(argv[1:])
        for prefix, answer in self.answers.items():
            if key.startswith(prefix):
                return answer
        return CliResult(1, "", "unknown command")


def test_onepassword_unlock_uses_stdin_and_lists(tmp_path: Path) -> None:
    rig, form, tool, scope = setup(tmp_path)
    token = "op-session-" + "t0k3n"
    items = [
        {
            "id": "abc",
            "title": "Bank",
            "vault": {"id": "v1"},
            "urls": [{"href": "https://bank.example/login", "primary": True}],
            "additional_information": "me@example.com",
        }
    ]
    cli = FakeCli(
        {
            "signin --raw": CliResult(0, token + "\n", ""),
            "item list --categories Login --format json": CliResult(0, json.dumps(items), ""),
            "item get abc --vault v1 --fields label=password --reveal": CliResult(
                0, PASSWORD + "\n", ""
            ),
        }
    )
    op = OnePassword(runner=cli, locate=lambda s, n: Path("/usr/bin/op"))
    tool.onepassword = op
    scope = scope.with_config(DictConfig({"browser": {"vault_sources": ["onepassword"]}}))  # type: ignore[call-arg, misc]
    go_to(rig, scope, BANK)
    out = call(tool, scope, Broker(secrets=[MASTER]), action="fill", handle="op-v1:abc")
    assert out["success"] is True and out["backend"] == "onepassword"
    assert list(form.filled.values()) == [PASSWORD]
    signin_argv, _env, stdin = cli.calls[0]
    assert signin_argv[1:] == ["signin", "--raw"] and stdin == MASTER
    for argv, _env2, _ in cli.calls:
        assert MASTER not in argv and token not in argv
    assert cli.calls[1][1]["OP_SESSION"] == token
    assert PASSWORD not in json.dumps(out)


def test_bitwarden_unlock_uses_env_and_reads_totp(tmp_path: Path) -> None:
    rig, form, tool, scope = setup(tmp_path, controls={"https://bank.example/2fa": OTP_FORM})
    token = "bw-session-" + "t0k3n"
    items = [
        {
            "id": "i1",
            "type": 1,
            "name": "Bank",
            "login": {"username": "me", "uris": [{"uri": "https://bank.example"}], "totp": "x"},
        },
        {"id": "n1", "type": 2, "name": "A note"},
    ]
    cli = FakeCli(
        {
            "unlock --raw --nointeraction --passwordenv INDUS_BW_MASTER": CliResult(0, token, ""),
            "list items --nointeraction": CliResult(0, json.dumps(items), ""),
            "get totp i1 --nointeraction": CliResult(0, "654321\n", ""),
        }
    )
    bw = Bitwarden(runner=cli, locate=lambda s, n: Path("/usr/bin/bw"))
    tool.bitwarden = bw
    scope = scope.with_config(DictConfig({"browser": {"vault_sources": ["bitwarden"]}}))  # type: ignore[call-arg, misc]
    unlocked = call(tool, scope, Broker(secrets=[MASTER]), action="unlock", backend="bitwarden")
    assert unlocked["success"] is True
    argv, env, stdin = cli.calls[0]
    assert MASTER not in argv and stdin is None and env["INDUS_BW_MASTER"] == MASTER
    listed = call(tool, scope, action="list")
    assert [i["handle"] for i in listed["items"]] == ["bw-i1"]
    assert listed["items"][0]["two_factor"] == "automatic"
    go_to(rig, scope, "https://bank.example/2fa")
    out = call(tool, scope, action="enter_code", handle="bw-i1")
    assert out["success"] is True and out["source"] == "bitwarden"
    assert list(form.filled.values()) == ["654321"]
    assert all(c[1].get("BW_SESSION") == token for c in cli.calls[1:])
    assert all("INDUS_BW_MASTER" not in c[1] for c in cli.calls[1:])


def test_unlock_without_a_surface(tmp_path: Path) -> None:
    _rig, _form, tool, scope = setup(tmp_path)
    tool.bitwarden = Bitwarden(runner=FakeCli({}), locate=lambda s, n: Path("/usr/bin/bw"))
    scope = scope.with_config(DictConfig({"browser": {"vault_sources": ["bitwarden"]}}))  # type: ignore[call-arg, misc]
    out = call(tool, scope, None, action="unlock", backend="bitwarden")
    assert out["error_type"] == "unlock_unavailable"


def test_cli_backend_missing_is_not_offered(tmp_path: Path) -> None:
    _rig, _form, tool, scope = setup(tmp_path)
    tool.onepassword = OnePassword(runner=FakeCli({}), locate=lambda s, n: None)
    scope = scope.with_config(DictConfig({"browser": {"vault_sources": ["local", "onepassword"]}}))  # type: ignore[call-arg, misc]
    assert [b.name for b in tool.backends(scope)] == ["local"]


# -- helpers ----------------------------------------------------------------------------------


def test_normalize_origin() -> None:
    assert normalize_origin("https://Bank.Example:443/login?x=1") == "https://bank.example"
    assert normalize_origin("http://bank.example:8080/") == "http://bank.example:8080"
    assert normalize_origin("https://bank.example.evil") != normalize_origin("https://bank.example")
    assert normalize_origin("about:blank") is None
    assert normalize_origin("javascript:alert(1)") is None


def test_classify_fields() -> None:
    assert classify("login", {"type": "password", "autocomplete": "current-password"}) == "password"
    assert classify("login", {"type": "password", "autocomplete": "new-password"}) is None
    assert classify("payment", {"type": "text", "autocomplete": "cc-number"}) == "cc_number"
    assert classify("payment", {"type": "text", "name": "card_cvv"}) == "cc_csc"
    assert classify("address", {"type": "text", "autocomplete": "postal-code"}) == "postal_code"
    assert classify("otp", {"type": "tel", "name": "verification_code"}) == "code"


def test_register_vault_tool(tmp_path: Path) -> None:
    rig = make_rig()
    registry = ToolRegistry()
    register_vault_tool(registry, rig.manager)
    entry = registry.get("browser_vault")
    assert entry is not None and entry.toolset == "browser"
