"""The Google Chat adapter in the shared contract suite, in both inbound modes (P16-13).

``google_chat``: signed HTTP callbacks into the adapter's listener app (no socket), tokens for the
project-number audience. ``google_chat_pubsub``: the Pub/Sub pull loop over the fake Pub/Sub API.
Both send through the fake Chat API with a real service-account token exchange.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from indus.gateway.adapters._http import ListenerSettings
from indus.gateway.adapters.base import Adapter
from indus.gateway.adapters.google_chat.adapter import (
    MAX_BODY_BYTES,
    GoogleChatAdapter,
    GoogleChatOptions,
)
from tests.contract.gateway.conftest import AdapterCase, CaseEnv, register_case
from tests.fakes.clock import FakeClock
from tests.fakes.platform.core import FakeChatPlatform
from tests.fakes.platform.google_chat_wire import (
    APP_USER,
    CALLBACK_PATH,
    MAX_LEN,
    PROJECT_NUMBER,
    SUBSCRIPTION,
    GoogleChatWire,
)


def _env(clock: FakeClock) -> CaseEnv:
    platform = FakeChatPlatform("google_chat", clock=clock, max_len=MAX_LEN, bot_user_id=APP_USER)
    return CaseEnv(clock, platform, GoogleChatWire(platform))


def _build_http(env: CaseEnv) -> Adapter:
    wire: GoogleChatWire = env.wire
    adapter = GoogleChatAdapter(
        clock=env.clock,
        account_json=wire.account_json,
        options=GoogleChatOptions(mode="http", audience=PROJECT_NUMBER),
        listener=ListenerSettings(port=0, max_body_bytes=MAX_BODY_BYTES, path=CALLBACK_PATH),
        transport=wire.transport(),
        serve=False,
        rng=lambda: 0.0,
    )
    assert adapter.listener is not None
    wire.attach(adapter.listener.app)
    return adapter


async def _pump_http(env: CaseEnv, adapter: Adapter) -> None:
    await env.wire.pump()
    assert isinstance(adapter, GoogleChatAdapter) and adapter.listener is not None
    await adapter.listener.idle()


def _build_pubsub(env: CaseEnv) -> Adapter:
    wire: GoogleChatWire = env.wire
    return GoogleChatAdapter(
        clock=env.clock,
        account_json=wire.account_json,
        options=GoogleChatOptions(mode="pubsub", subscription=SUBSCRIPTION),
        transport=wire.transport(),
        rng=lambda: 0.0,
    )


# Chat apps get no event when a message is edited.
_LACKS = frozenset({"edit"})


def _case(name: str, build: Callable[[CaseEnv], Adapter], **kw: Any) -> AdapterCase:
    return AdapterCase(
        name,
        _env,
        build,
        user_id="users/200",
        group_id="spaces/G300",
        thread_id="spaces/G300/threads/T400",
        bot_id="users/500",
        lacks=_LACKS,
        **kw,
    )


GOOGLE_CHAT_CASE = register_case(_case("google_chat", _build_http, pump=_pump_http))
GOOGLE_CHAT_PUBSUB_CASE = register_case(_case("google_chat_pubsub", _build_pubsub))
