"""The Teams adapter in the shared contract suite, over the fake Bot Framework (P16-11)."""

from __future__ import annotations

from indus.gateway.adapters._http import ListenerSettings
from indus.gateway.adapters.base import Adapter
from indus.gateway.adapters.teams.adapter import MAX_BODY_BYTES, WEBHOOK_PATH, TeamsAdapter
from tests.contract.gateway.conftest import AdapterCase, CaseEnv, register_case
from tests.fakes.clock import FakeClock
from tests.fakes.platform.core import FakeChatPlatform
from tests.fakes.platform.teams_wire import APP_ID, BOT_NAME, MAX_LEN, TENANT_ID, TeamsWire


def _env(clock: FakeClock) -> CaseEnv:
    platform = FakeChatPlatform(
        "teams", clock=clock, max_len=MAX_LEN, bot_user_id=f"28:{APP_ID}", bot_name=BOT_NAME
    )
    return CaseEnv(clock, platform, TeamsWire(platform))


def _build(env: CaseEnv) -> Adapter:
    wire: TeamsWire = env.wire
    adapter = TeamsAdapter(
        clock=env.clock,
        app_id=APP_ID,
        app_secret=wire.secret,
        tenant_id=TENANT_ID,
        listener=ListenerSettings(port=0, max_body_bytes=MAX_BODY_BYTES, path=WEBHOOK_PATH),
        transport=wire.transport(),
        serve=False,
    )
    wire.attach(adapter.listener.app)
    return adapter


async def _pump(env: CaseEnv, adapter: Adapter) -> None:
    await env.wire.pump()
    assert isinstance(adapter, TeamsAdapter)
    await adapter.listener.idle()


TEAMS_CASE = register_case(AdapterCase("teams", _env, _build, pump=_pump))
