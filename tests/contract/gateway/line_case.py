"""The LINE adapter in the shared contract suite (P16-23): our adapter over the LINE wire fake.

LINE ids are a letter and 32 hex digits (``U`` users, ``C`` groups). LINE has no threads, no
bot-authored inbound messages, no edits and no captions on images: the case says so (``lacks``)
and the inbound check expects nothing for those shapes.
"""

from __future__ import annotations

from indus.gateway.adapters._http import ListenerSettings
from indus.gateway.adapters.base import Adapter, utf16_units
from indus.gateway.adapters.line.adapter import (
    MAX_BODY_BYTES,
    WEBHOOK_PATH,
    LineAdapter,
    LineOptions,
)
from indus.gateway.adapters.line.api import LineApi
from tests.contract.gateway.conftest import AdapterCase, CaseEnv, register_case
from tests.fakes.clock import FakeClock
from tests.fakes.platform.core import FakeChatPlatform
from tests.fakes.platform.line_wire import LineWire

# Built at runtime: never a credential-shaped literal in the source.
TOKEN = "".join(["line", "-token-", "a1b2" * 4])
SECRET = "".join(["line", "-secret-", "c3d4" * 4])
BOT_ID = "U" + "f" * 32
USER_ID = "U" + "a" * 29 + "200"
GROUP_ID = "C" + "b" * 29 + "300"
OTHER_BOT_ID = "U" + "e" * 29 + "500"
MEDIA_BASE = "https://line.fake.test"


def make_env(clock: FakeClock) -> CaseEnv:
    platform = FakeChatPlatform(
        "line", clock=clock, max_len=5000, len_fn=utf16_units, bot_user_id=BOT_ID
    )
    return CaseEnv(clock, platform, LineWire(platform, token=TOKEN, secret=SECRET))


def build_adapter(env: CaseEnv, options: LineOptions | None = None) -> LineAdapter:
    wire: LineWire = env.wire
    api = LineApi(TOKEN, client_factory=wire.client)
    settings = ListenerSettings(port=0, max_body_bytes=MAX_BODY_BYTES, path=WEBHOOK_PATH)
    adapter = LineAdapter(
        api,
        channel_secret=SECRET,
        listener=settings,
        clock=env.clock,
        secrets=(TOKEN,),
        options=options or LineOptions(media_base=MEDIA_BASE),
        serve=False,
    )
    wire.attach_listener(adapter.listener)
    return adapter


def build(env: CaseEnv) -> Adapter:
    return build_adapter(env)


async def pump(env: CaseEnv, adapter: Adapter) -> None:
    await env.wire.pump()
    assert isinstance(adapter, LineAdapter)
    await adapter.listener.idle()


LINE_CASE = register_case(
    AdapterCase(
        "line",
        make_env,
        build,
        user_id=USER_ID,
        group_id=GROUP_ID,
        thread_id="400",
        bot_id=OTHER_BOT_ID,
        pump=pump,
        lacks=frozenset({"thread", "bot", "edit", "caption"}),
    )
)
