"""Settings and model methods (P12-31): get/set round trips, session switches, a deferred model
change, confirmation for flagged models, provider keys in the secret store."""

from __future__ import annotations

import asyncio
from pathlib import Path

from indus.foundation.config import loader
from indus.foundation.scope import Scope
from indus.protocol.errors import CONFIRM_REQUIRED, INVALID_PARAMS, UNKNOWN_PARAM_KEY
from indus.providers import bundled
from tests.contract.tui_bridge._harness import open_rig
from tests.fakes.agent import eventually
from tests.fakes.clock import FakeClock
from tests.fakes.model import Finish, Stall, Text, Turn


def test_client_settings_round_trip(scope: Scope, fake_clock: FakeClock, tmp_path: Path) -> None:
    async def main() -> None:
        async with open_rig(scope, fake_clock, tmp_path) as rig:
            full = (await rig.ok("config.get", key="full"))["value"]
            assert full["theme"] == "auto" and full["mouse"] is False and full["verbose"] == "all"
            assert (await rig.ok("config.get", key="mtime"))["value"] is None
            saved = await rig.ok("config.set", key="theme", value="ember")
            assert saved == {"key": "theme", "value": "ember", "applied": "now", "saved": True}
            await rig.ok("config.set", key="mouse", value=True)
            await rig.ok("config.set", key="busy", value="QUEUE")
            assert (await rig.ok("config.get", key="theme"))["value"] == "ember"
            assert (await rig.ok("config.get", key="tui.mouse"))["value"] is True
            assert (await rig.ok("config.get", key="display.busy_input_mode"))["value"] == "queue"
            assert isinstance((await rig.ok("config.get", key="mtime"))["value"], float)
            on_disk = loader.load(scope)
            assert on_disk.get("tui.theme") == "ember" and on_disk.get("tui.mouse") is True
            wrong = await rig.err("config.set", key="mouse", value="yes")
            assert wrong["code"] == INVALID_PARAMS.code and wrong["data"]["path"] == "value"
            nope = await rig.err("config.set", key="colour", value=1)
            assert nope["code"] == INVALID_PARAMS.code and nope["data"]["path"] == "key"
            typo = await rig.err("config.set", key="theme", value="x", sesion_id="s")
            assert typo["code"] == UNKNOWN_PARAM_KEY.code and typo["data"]["path"] == "sesion_id"
            secret = await rig.err("config.get", key="providers")
            assert secret["code"] == INVALID_PARAMS.code

    asyncio.run(main())


def test_session_switches(scope: Scope, fake_clock: FakeClock, tmp_path: Path) -> None:
    async def main() -> None:
        async with open_rig(scope, fake_clock, tmp_path) as rig:
            sid = (await rig.ok("session.create"))["session_id"]
            verbose = await rig.ok("config.set", key="verbose", value=True, session_id=sid)
            assert verbose["value"] == "verbose" and verbose["saved"] is False
            view = rig.bridge.view(sid)
            assert view is not None and view.tool_mode == "verbose"
            assert rig.recorder.of("session.info")[-1]["tool_progress"] == "verbose"
            yolo = await rig.ok("config.set", key="yolo", value=True, session_id=sid)
            assert yolo["value"] is True
            fast = await rig.ok("config.set", key="fast", value="fast", session_id=sid)
            assert fast["value"] == "fast"
            live = (await rig.ok("config.get", key="full", session_id=sid))["value"]
            assert live["yolo"] is True and live["fast"] == "fast" and live["verbose"] == "verbose"
            alone = await rig.err("config.set", key="yolo", value=True)
            assert alone["data"]["path"] == "session_id"
            await rig.ok("config.set", key="yolo", value=False, session_id=sid)

    asyncio.run(main())


def test_model_change_waits_for_the_turn_and_flagged_models_ask_first(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    (scope.home / "config.yaml").write_text(
        "tui:\n  confirm_models:\n    - big-model\n", encoding="utf-8"
    )
    slow = Turn((Text(("thinking",)), Stall(50.0), Finish("stop")))

    async def main() -> None:
        async with open_rig(scope, fake_clock, tmp_path, slow) as rig:
            sid = (await rig.ok("session.create"))["session_id"]
            asked = await rig.err("config.set", key="model", value="big-model", session_id=sid)
            assert asked["code"] == CONFIRM_REQUIRED.code
            await rig.ok("prompt.submit", session_id=sid, text="go")
            deferred = await rig.ok(
                "config.set", key="model", value="big-model", session_id=sid, confirm=True
            )
            assert deferred["applied"] == "deferred"
            view = rig.bridge.view(sid)
            assert view is not None and view.pending_model == "big-model"
            await rig.ok("session.interrupt", session_id=sid, hard=True)
            await rig.turn_done()
            await rig.idle(sid)
            await eventually(lambda: view.pending_model is None)  # tried once the turn ended

    asyncio.run(main())


def test_provider_keys_live_in_the_secret_store(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    keyed = next(
        p for p in bundled.PROFILES if p.auth_type == "api_key" and p.env_vars and not p.hidden
    )
    key = "k-" + "Fake0123456789abcdef"

    async def main() -> None:
        async with open_rig(scope, fake_clock, tmp_path) as rig:
            options = await rig.ok("model.options")
            row = next(p for p in options["providers"] if p["slug"] == keyed.name)
            assert row["authenticated"] is False
            saved = await rig.ok("model.save_key", slug=keyed.name, api_key=key)
            assert saved["provider"]["authenticated"] is True
            assert scope.secrets is not None and scope.secrets.get(keyed.env_vars[0]) == key
            assert key not in str(rig.recorder.frames)
            gone = await rig.ok("model.disconnect", slug=keyed.name)
            assert gone == {"removed": True}
            assert scope.secrets.get(keyed.env_vars[0]) is None
            again = await rig.ok("model.disconnect", slug=keyed.name)
            assert again == {"removed": False}
            unknown = await rig.err("model.save_key", slug="no-such", api_key=key)
            assert unknown["data"]["path"] == "slug"
            reload = await rig.ok("reload.mcp")
            assert reload["reloaded"] is False

    asyncio.run(main())
