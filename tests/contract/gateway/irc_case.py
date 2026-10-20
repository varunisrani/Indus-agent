"""The IRC adapter in the shared contract suite (P16-27): our adapter on the fake IRC server.

Nicks are users and DM chats, ``#`` names are channels. IRC has no threads, edits or attachments:
the case says so (``lacks``). Bot authors arrive with the IRCv3 ``bot`` tag.
"""

from __future__ import annotations

from indus.gateway.adapters.base import Adapter
from indus.gateway.adapters.irc.adapter import IrcAdapter, IrcOptions
from indus.gateway.adapters.irc.protocol import utf8_bytes
from tests.contract.gateway.conftest import AdapterCase, CaseEnv, register_case
from tests.fakes.clock import FakeClock
from tests.fakes.platform.core import FakeChatPlatform
from tests.fakes.platform.irc_wire import IrcWire

NICK = "IndusBot"
CHANNEL = "#indus"


def make_env(clock: FakeClock) -> CaseEnv:
    platform = FakeChatPlatform(
        "irc", clock=clock, max_len=450, len_fn=utf8_bytes, bot_user_id=NICK, bot_name=NICK
    )
    return CaseEnv(clock, platform, IrcWire(platform))


def build_adapter(env: CaseEnv, options: IrcOptions | None = None, **kw: object) -> IrcAdapter:
    wire: IrcWire = env.wire
    opts = options or IrcOptions(server="irc.fake.test", nickname=NICK, channels=(CHANNEL,))
    return IrcAdapter(opts, clock=env.clock, connector=wire.connect, **kw)  # type: ignore[arg-type]


def build(env: CaseEnv) -> Adapter:
    return build_adapter(env)


async def pump(env: CaseEnv, adapter: Adapter) -> None:
    await env.wire.pump()
    await env.clock.run_until_idle()


IRC_CASE = register_case(
    AdapterCase(
        "irc",
        make_env,
        build,
        user_id="ana",
        group_id="#ops",
        thread_id="400",
        bot_id="otherbot",
        pump=pump,
        lacks=frozenset({"thread", "edit", "media"}),
    )
)
