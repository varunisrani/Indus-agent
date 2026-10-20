"""The SMS (Twilio) adapter in the shared contract suite (P16-10): our adapter over the fake
Twilio API, its listener fed in-process by the wire's signed webhooks (no socket)."""

from __future__ import annotations

from indus.gateway.adapters._http import ListenerSettings
from indus.gateway.adapters.base import Adapter, utf16_units
from indus.gateway.adapters.sms.adapter import (
    DEFAULT_PATH,
    MAX_BODY_BYTES,
    SmsAdapter,
    TwilioSettings,
)
from tests.contract.gateway.conftest import AdapterCase, CaseEnv, register_case
from tests.fakes.clock import FakeClock
from tests.fakes.platform.core import FakeChatPlatform
from tests.fakes.platform.sms_wire import (
    ACCOUNT_SID,
    AUTH_TOKEN,
    MAX_LEN,
    PHONE_NUMBER,
    PUBLIC_URL,
    TwilioWire,
)


def twilio_settings(**kw: str) -> TwilioSettings:
    values = {
        "account_sid": ACCOUNT_SID,
        "auth_token": AUTH_TOKEN,
        "phone_number": PHONE_NUMBER,
        "public_url": PUBLIC_URL,
        "base_url": TwilioWire.base_url,
        **kw,
    }
    return TwilioSettings(**values)


def make_env(clock: FakeClock) -> CaseEnv:
    platform = FakeChatPlatform(
        "sms", clock=clock, max_len=MAX_LEN, len_fn=utf16_units, bot_user_id=PHONE_NUMBER
    )
    return CaseEnv(clock, platform, TwilioWire(platform))


def build(env: CaseEnv, *, send_only: bool = False, **kw: str) -> SmsAdapter:
    wire: TwilioWire = env.wire
    adapter = SmsAdapter(
        clock=env.clock,
        twilio=twilio_settings(**kw),
        listener=ListenerSettings(port=0, max_body_bytes=MAX_BODY_BYTES, path=DEFAULT_PATH),
        client_factory=wire.client,
        serve=False,
        send_only=send_only,
    )
    wire.attach(adapter.listener.app)
    return adapter


async def pump(env: CaseEnv, adapter: Adapter) -> None:
    """Deliver the queued webhooks and wait for the events they started."""
    assert isinstance(adapter, SmsAdapter)
    await env.wire.pump()
    await adapter.listener.idle()


SMS_CASE = register_case(
    AdapterCase(
        "sms",
        make_env,
        build,
        user_id="+15550000200",
        group_id="+15550000300",
        thread_id="400",
        bot_id="+15550000500",
        pump=pump,
        # SMS is 1:1 text: no groups, threads, bot marks or edits.
        lacks=frozenset({"group", "thread", "bot", "edit"}),
    )
)
