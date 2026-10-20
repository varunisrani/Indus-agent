"""The ntfy adapter in the shared contract suite (P16-26): our adapter over the ntfy fake.

A topic is both the chat and the "user" (ntfy has no authenticated senders). ntfy has no groups,
threads, bot authors or edits, and the adapter takes no attachments: the case says so
(``lacks``), so the group command arrives on the topic and the media items publish nothing.
"""

from __future__ import annotations

from indus.gateway.adapters.base import Adapter
from indus.gateway.adapters.ntfy.adapter import MAX_BYTES, NtfyAdapter, NtfyApi, utf8_bytes
from tests.contract.gateway.conftest import AdapterCase, CaseEnv, register_case
from tests.fakes.clock import FakeClock
from tests.fakes.platform.core import FakeChatPlatform
from tests.fakes.platform.ntfy_wire import NtfyWire

# Built at runtime: never a credential-shaped literal in the source.
TOKEN = "".join(["ntfy", "-access-", "e5f6" * 4])
TOPIC = "indus-in"
OTHER_TOPIC = "ops-room"


def make_env(clock: FakeClock) -> CaseEnv:
    platform = FakeChatPlatform("ntfy", clock=clock, max_len=MAX_BYTES, len_fn=utf8_bytes)
    return CaseEnv(clock, platform, NtfyWire(platform, token=TOKEN))


def build_adapter(env: CaseEnv, **kw: object) -> NtfyAdapter:
    wire: NtfyWire = env.wire
    api = NtfyApi(wire.base_url, credential=TOKEN, client_factory=wire.client)
    topics = kw.pop("topics", (TOPIC,))
    return NtfyAdapter(api, topics=topics, clock=env.clock, secrets=(TOKEN,), **kw)  # type: ignore[arg-type]


def build(env: CaseEnv) -> Adapter:
    return build_adapter(env)


async def pump(env: CaseEnv, adapter: Adapter) -> None:
    await env.wire.pump()
    await env.clock.run_until_idle()


NTFY_CASE = register_case(
    AdapterCase(
        "ntfy",
        make_env,
        build,
        user_id=TOPIC,
        group_id=OTHER_TOPIC,
        thread_id="400",
        bot_id="other-bot",
        pump=pump,
        lacks=frozenset({"group", "thread", "bot", "edit", "media"}),
    )
)
