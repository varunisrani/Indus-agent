"""The WeCom callback adapter's contract case (P16-18): the real adapter, its listener served
in-process, over the callback wire fake. Needs the ``cryptography`` package (the ``wecom`` extra;
on Linux it also comes with keyring): the case is not registered without it."""

from __future__ import annotations

import importlib.util

from indus.gateway.adapters._http import ListenerSettings
from indus.gateway.adapters.base import Adapter
from indus.gateway.adapters.wecom.callback import WeComCallbackAdapter
from tests.contract.gateway.conftest import AdapterCase, CaseEnv, register_case
from tests.fakes.clock import FakeClock
from tests.fakes.platform.core import FakeChatPlatform
from tests.fakes.platform.wecom_wire import AGENT_ID, CORP_ID, WeComCallbackWire


def _make_env(clock: FakeClock) -> CaseEnv:
    platform = FakeChatPlatform("wecom_callback", clock=clock, max_len=2048, len_fn=_utf8)
    return CaseEnv(clock, platform, WeComCallbackWire(platform))


def _utf8(text: str) -> int:
    return len(text.encode("utf-8"))


def _build(env: CaseEnv) -> Adapter:
    wire: WeComCallbackWire = env.wire
    adapter = WeComCallbackAdapter(
        clock=env.clock,
        corp_id=CORP_ID,
        corp_secret=wire.corp_secret,
        agent_id=AGENT_ID,
        token=wire.token,
        encoding_aes_key=wire.aes_key,
        listener=ListenerSettings(port=0, max_body_bytes=65_536, path="/wecom/callback"),
        transport=wire.transport(),
        api_base=wire.base_url,
        serve=False,
    )
    wire.attach(adapter.listener.app)
    return adapter


async def _pump(env: CaseEnv, adapter: Adapter) -> None:
    await env.wire.pump()
    assert isinstance(adapter, WeComCallbackAdapter)
    await adapter.listener.idle()


if importlib.util.find_spec("cryptography") is not None:
    WECOM_CALLBACK_CASE = register_case(
        AdapterCase(
            "wecom_callback",
            _make_env,
            _build,
            pump=_pump,
            # People write to an app one to one: no groups, threads, edits, bot flags,
            # documents or photo captions in callback mode.
            lacks=frozenset({"group", "thread", "bot", "edit", "document", "caption"}),
        )
    )
