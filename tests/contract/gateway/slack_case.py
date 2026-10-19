"""The Slack adapter's case in the shared adapter contract (P15-35).

The adapter runs over the Slack wire fake through its library seam (`SlackSdk`), so the suite needs
no Slack library. Ids are Slack-shaped: a user ``U…`` (its DM is opened with
``conversations.open``), a channel ``C…`` and a thread root ``ts``.
"""

from __future__ import annotations

from indus.gateway.adapters.base import Adapter
from indus.gateway.adapters.slack.adapter import MAX_LEN, SlackAdapter, SlackSdk
from tests.contract.gateway.conftest import AdapterCase, CaseEnv, register_case
from tests.fakes.clock import FakeClock
from tests.fakes.platform.slack_wire import SlackPlatform, SlackWire


def wire_sdk(wire: SlackWire) -> SlackSdk:
    """A `SlackSdk` whose app and Socket Mode handler are the wire fake's."""
    return SlackSdk(
        make_app=lambda _token, _proxy: wire.app,
        make_socket=lambda _app, app_token, _proxy: wire.handler(app_token),
    )


def make_env(clock: FakeClock) -> CaseEnv:
    platform = SlackPlatform("slack", clock=clock, max_len=MAX_LEN, bot_user_id="U0BOT")
    return CaseEnv(clock, platform, SlackWire(platform))


def build(env: CaseEnv) -> Adapter:
    return SlackAdapter(
        clock=env.clock,
        bot_token="bot-" + "contract",
        app_token="app-" + "contract",
        sdk=wire_sdk(env.wire),
    )


async def pump(env: CaseEnv, adapter: Adapter) -> None:
    await env.wire.dispatch_pending()


SLACK_CASE = register_case(
    AdapterCase(
        "slack",
        make_env,
        build,
        user_id="U200",
        group_id="C300",
        thread_id="1700000000.000400",
        bot_id="B500",
        pump=pump,
    )
)
