"""The Feishu / Lark adapter's contract cases (P16-14): the real adapter over its wire fake, once
over the long connection and once in webhook mode (signed, encrypted callbacks)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from indus.gateway.adapters.base import Adapter
from tests.contract.gateway.conftest import AdapterCase, CaseEnv, register_case
from tests.fakes.clock import FakeClock
from tests.fakes.platform.feishu_wire import FeishuWire

USER, GROUP, THREAD, PEER_BOT = "ou_user200", "oc_group300", "omt_thread400", "ou_peerbot500"
# Feishu has no event for an edited message.
LACKS = frozenset({"edit"})


def _make_env(clock: FakeClock) -> CaseEnv:
    wire = FeishuWire(clock=clock)
    return CaseEnv(clock, wire.platform, wire)


def _build(env: CaseEnv) -> Adapter:
    return env.wire.adapter()


def _build_webhook(env: CaseEnv) -> Adapter:
    return env.wire.webhook_adapter()


async def _pump_webhook(env: CaseEnv, adapter: Adapter) -> None:
    await env.wire.pump()


def _case(
    name: str,
    build: Callable[[CaseEnv], Adapter],
    pump: Callable[[CaseEnv, Adapter], Awaitable[None]] | None = None,
) -> AdapterCase:
    return AdapterCase(
        name,
        _make_env,
        build,
        user_id=USER,
        group_id=GROUP,
        thread_id=THREAD,
        bot_id=PEER_BOT,
        pump=pump,
        lacks=LACKS,
    )


FEISHU_CASE = register_case(_case("feishu", _build))
FEISHU_WEBHOOK_CASE = register_case(_case("feishu_webhook", _build_webhook, _pump_webhook))
