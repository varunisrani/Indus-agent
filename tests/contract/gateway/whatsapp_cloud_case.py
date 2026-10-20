"""The WhatsApp Cloud adapter in the shared contract suite (P16-04): our adapter over the fake
Graph API, its listener fed in-process by the wire's signed webhooks (no socket)."""

from __future__ import annotations

from indus.gateway.adapters._http import ListenerSettings
from indus.gateway.adapters.base import Adapter
from indus.gateway.adapters.whatsapp_cloud.adapter import (
    DEFAULT_PATH,
    MAX_BODY_BYTES,
    CloudSettings,
    WhatsAppCloudAdapter,
)
from tests.contract.gateway.conftest import AdapterCase, CaseEnv, register_case
from tests.fakes.clock import FakeClock
from tests.fakes.platform.core import FakeChatPlatform
from tests.fakes.platform.whatsapp_cloud_wire import (
    APP_SECRET,
    BUSINESS_NUMBER,
    MAX_LEN,
    PHONE_NUMBER_ID,
    TOKEN,
    VERIFY_TOKEN,
    WhatsAppCloudWire,
)


def cloud_settings(**kw: str) -> CloudSettings:
    values = {
        "access_token": TOKEN,
        "phone_number_id": PHONE_NUMBER_ID,
        "app_secret": APP_SECRET,
        "verify_token": VERIFY_TOKEN,
        "base_url": WhatsAppCloudWire.base_url,
        **kw,
    }
    return CloudSettings(**values)


def make_env(clock: FakeClock) -> CaseEnv:
    platform = FakeChatPlatform(
        "whatsapp_cloud", clock=clock, max_len=MAX_LEN, bot_user_id=BUSINESS_NUMBER
    )
    return CaseEnv(clock, platform, WhatsAppCloudWire(platform))


def build(env: CaseEnv, *, send_only: bool = False, **kw: str) -> WhatsAppCloudAdapter:
    wire: WhatsAppCloudWire = env.wire
    adapter = WhatsAppCloudAdapter(
        clock=env.clock,
        cloud=cloud_settings(**kw),
        listener=ListenerSettings(port=0, max_body_bytes=MAX_BODY_BYTES, path=DEFAULT_PATH),
        client_factory=wire.client,
        serve=False,
        send_only=send_only,
    )
    wire.attach(adapter.listener.app)
    return adapter


async def pump(env: CaseEnv, adapter: Adapter) -> None:
    """Deliver the queued webhooks and wait for the events they started."""
    assert isinstance(adapter, WhatsAppCloudAdapter)
    await env.wire.pump()
    await adapter.listener.idle()


WHATSAPP_CLOUD_CASE = register_case(
    AdapterCase(
        "whatsapp_cloud",
        make_env,
        build,
        user_id="15550000200",
        group_id="15550000300",
        thread_id="400",
        bot_id="15550000500",
        pump=pump,
        # Cloud API chats are 1:1; Meta marks no bots and sends no edits.
        lacks=frozenset({"group", "thread", "bot", "edit"}),
    )
)
