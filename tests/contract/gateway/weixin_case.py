"""The Weixin adapter in the shared contract suite (P16-19): our adapter over the iLink fake.

iLink lets a bot answer only conversations it holds a ``context_token`` for, so the case starts
with tokens for the test user and group (as if each had written first); the "reply without a
token" rule has its own unit test. Weixin has no threads and no edits (``lacks``); group messages
are on (``group_policy: open``) and the bot's name marks a mention.
"""

from __future__ import annotations

from indus.gateway.adapters.base import Adapter
from indus.gateway.adapters.weixin.adapter import ContextTokens, WeixinAdapter, WeixinOptions
from tests.contract.gateway.conftest import AdapterCase, CaseEnv, register_case
from tests.fakes.clock import FakeClock
from tests.fakes.platform.core import FakeChatPlatform
from tests.fakes.platform.weixin_wire import (
    BOT_ACCOUNT,
    BOT_NAME,
    MAX_CHARS,
    WeixinWire,
    fake_token,
)

USER = "wxid_ana01"
GROUP = "4410001@chatroom"


def make_env(clock: FakeClock) -> CaseEnv:
    platform = FakeChatPlatform(
        "weixin", clock=clock, max_len=MAX_CHARS, bot_user_id=BOT_ACCOUNT, bot_name=BOT_NAME
    )
    return CaseEnv(clock, platform, WeixinWire(platform))


def build(env: CaseEnv) -> Adapter:
    wire: WeixinWire = env.wire
    tokens = ContextTokens(seed={USER: wire.token_for(USER), GROUP: wire.token_for(GROUP)})
    return WeixinAdapter(
        clock=env.clock,
        token=fake_token(),
        account_id=BOT_ACCOUNT,
        options=WeixinOptions(group_policy="open", mention_names=(BOT_NAME,)),
        tokens=tokens,
        transport=wire.transport(),
    )


WEIXIN_CASE = register_case(
    AdapterCase(
        "weixin",
        make_env,
        build,
        user_id=USER,
        group_id=GROUP,
        thread_id="400",
        bot_id="wxid_otherbot",
        lacks=frozenset({"thread", "edit"}),
    )
)
