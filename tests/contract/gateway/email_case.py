"""The email adapter in the shared contract suite, over in-process IMAP/SMTP fakes (P16-09)."""

from __future__ import annotations

from typing import Any

from indus.gateway.adapters.base import Adapter
from indus.gateway.adapters.email.adapter import EmailAdapter, EmailSettings
from tests.contract.gateway.conftest import AdapterCase, CaseEnv, register_case
from tests.fakes.clock import FakeClock
from tests.fakes.platform.core import FakeChatPlatform
from tests.fakes.platform.email_wire import AUTHSERV_ID, BOT_ADDRESS, MAX_LEN, EmailWire

# Built at runtime: never a credential-shaped literal in the source (builder rules).
PASSWORD = "-".join(["contract", "mail", "pass"])


async def inline(fn: Any, *args: Any) -> Any:
    return fn(*args)


def _env(clock: FakeClock) -> CaseEnv:
    platform = FakeChatPlatform("email", clock=clock, max_len=MAX_LEN, bot_user_id=BOT_ADDRESS)
    return CaseEnv(clock, platform, EmailWire(platform, password=PASSWORD))


def _build(env: CaseEnv) -> Adapter:
    wire: EmailWire = env.wire
    mail = EmailSettings(
        address=BOT_ADDRESS,
        password=PASSWORD,
        imap_host="imap.indus.test",
        smtp_host="smtp.indus.test",
        authserv_id=AUTHSERV_ID,
        allow_from=frozenset({"*"}),
    )
    return EmailAdapter(
        clock=env.clock,
        mail=mail,
        imap_factory=wire.open_imap,
        smtp_factory=wire.open_smtp,
        runner=inline,
    )


async def _pump(env: CaseEnv, adapter: Adapter) -> None:
    await env.clock.advance_and_settle(61.0)  # one poll interval


EMAIL_CASE = register_case(
    AdapterCase(
        "email",
        _env,
        _build,
        user_id="ana@example.org",
        group_id="team@example.org",
        thread_id="400",
        bot_id="robot@example.org",
        pump=_pump,
        # Mail has no groups, threads or edits; automated mail is dropped, never answered.
        lacks=frozenset({"group", "thread", "bot", "edit"}),
    )
)
