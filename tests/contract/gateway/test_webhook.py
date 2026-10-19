"""P15-32: the webhook receiver — signatures, limits, replay, coalescing, delivery, tool floor."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.scope import Scope
from indus.gateway.http.webhook import TIMESTAMP_WINDOW_S
from indus.gateway.http.webhook_rules import RouteError
from indus.state.webhook_subscriptions import SubscriptionStore, sign_request
from indus.tools.builtin import load_builtins
from indus.tools.registry import ToolRegistry
from indus.tools.toolsets import WEBHOOK_FLOOR
from tests.contract.gateway._http_kit import HookKit, build_webhook, make_service, until, within
from tests.fakes.clock import FakeClock
from tests.fakes.model import text_turn

PAYLOADS = Path(__file__).parents[2] / "fixtures" / "gateway" / "webhook_payloads"
SECRET_NAME = "INDUS_WEBHOOK_TEST"
SECRET = "s3cret-" + "x" * 24  # built at runtime: not a credential shape


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def payload(name: str) -> bytes:
    return (PAYLOADS / name).read_bytes()


def route(**fields: Any) -> dict[str, Any]:
    return {"secret": SECRET_NAME, **fields}


def signed(kit: HookKit, auth: str, body: bytes, **extra: str) -> dict[str, str]:
    now = int(kit.clock.now().timestamp())
    return {"Content-Type": "application/json", **sign_request(auth, SECRET, body, now), **extra}


async def post(kit: HookKit, name: str, body: bytes, headers: dict[str, str]) -> Any:
    async with kit.client() as client:
        return await within(client.post(f"/webhooks/{name}", content=body, headers=headers))


async def hook(scope: Scope, clock: FakeClock, tmp_path: Path, *turns: Any, **kw: Any) -> HookKit:
    kw.setdefault("secrets", {SECRET_NAME: SECRET})
    return await build_webhook(scope, clock, tmp_path, *turns, **kw)


# -- signatures ---------------------------------------------------------------------------------


@pytest.mark.parametrize("auth", ["github", "gitlab", "hmac", "hmac_ts", "auto"])
def test_a_bad_signature_is_401_and_a_good_one_runs_the_agent(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path, auth: str
) -> None:
    async def main() -> None:
        kit = await hook(
            scope,
            fake_clock,
            tmp_path,
            text_turn("Looks fine."),
            routes={"gh": route(auth=auth, prompt="PR {pull_request.title}", deliver="telegram:1")},
        )
        try:
            body = payload("github_pull_request.json")
            sender = "hmac_ts" if auth == "auto" else auth
            good = signed(kit, sender, body, **{"X-GitHub-Event": "pull_request"})
            forged = {
                k: (v[:-4] + "0000" if k != "X-Indus-Timestamp" else v)
                for k, v in good.items()
                if k != "Content-Type"
            }
            bad = await post(kit, "gh", body, forged)
            assert bad.status_code == 401
            missing = await post(kit, "gh", body, {"Content-Type": "application/json"})
            assert missing.status_code == 401
            if auth != "gitlab":  # a GitLab token does not cover the body
                tampered = await post(kit, "gh", body + b" ", good)
                assert tampered.status_code == 401
            ok = await post(kit, "gh", body, good)
            assert ok.status_code == 202, ok.text
            assert ok.json()["status"] == "accepted"
            await kit.receiver.drain()
            assert len(kit.server.model_requests) == 1
            sent_prompt = str(kit.server.model_requests[0].body)
            assert "PR Speed up the nightly import" in sent_prompt
            assert [(s.target, s.text) for s in kit.deliver.sent] == [("telegram:1", "Looks fine.")]
        finally:
            await kit.close()

    run(main())


def test_the_timestamped_scheme_refuses_stale_requests_and_never_falls_back(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await hook(
            scope,
            fake_clock,
            tmp_path,
            routes={"ts": route(deliver_only=True, deliver="local", prompt="x")},
        )
        try:
            body = payload("generic_alert.json")
            now = int(fake_clock.now().timestamp())
            stale = sign_request("hmac_ts", SECRET, body, now - int(TIMESTAMP_WINDOW_S) - 1)
            assert (await post(kit, "ts", body, stale)).status_code == 401
            future = sign_request("hmac_ts", SECRET, body, now + int(TIMESTAMP_WINDOW_S) + 1)
            assert (await post(kit, "ts", body, future)).status_code == 401
            edge = sign_request("hmac_ts", SECRET, body, now - int(TIMESTAMP_WINDOW_S) + 1)
            assert (await post(kit, "ts", body, edge)).status_code == 200
            # A timestamped header commits to the timestamped check: the plain HMAC of the same
            # body does not get it in once the timestamp is stripped.
            plain = sign_request("hmac", SECRET, body, now)
            mixed = {**plain, "X-Indus-Signature-Ts": stale["X-Indus-Signature-Ts"]}
            assert (await post(kit, "ts", body, mixed)).status_code == 401
        finally:
            await kit.close()

    run(main())


# -- replay, rate, size -------------------------------------------------------------------------


def test_a_replayed_delivery_is_answered_without_a_second_turn(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await hook(
            scope,
            fake_clock,
            tmp_path,
            text_turn("once"),
            text_turn("twice"),
            routes={"gh": route(auth="github")},
        )
        try:
            body = payload("github_pull_request.json")
            headers = signed(kit, "github", body, **{"X-GitHub-Delivery": "d-1"})
            assert (await post(kit, "gh", body, headers)).status_code == 202
            again = await post(kit, "gh", body, headers)
            assert again.status_code == 200 and again.json()["status"] == "duplicate"
            await kit.receiver.drain()
            assert len(kit.server.model_requests) == 1
            # No delivery id at all: the body's hash stands in, so a replayed body is caught too.
            plain = signed(kit, "github", body)
            assert (await post(kit, "gh", body, plain)).status_code == 202
            replay = await post(kit, "gh", body, plain)
            assert replay.json()["status"] == "duplicate"
            await kit.receiver.drain()
            assert len(kit.server.model_requests) == 2
            fake_clock.advance(3600.0)  # an hour later the id may be used again
            assert (await post(kit, "gh", body, headers)).status_code != 200
        finally:
            await kit.close()

    run(main())


def test_the_31st_request_in_60_s_is_429(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await hook(
            scope,
            fake_clock,
            tmp_path,
            routes={"fast": route(auth="hmac", deliver_only=True, deliver="local", prompt="{n}")},
        )
        try:
            codes = []
            for n in range(31):
                body = json.dumps({"n": n}).encode()
                codes.append((await post(kit, "fast", body, signed(kit, "hmac", body))).status_code)
            assert codes[:30] == [200] * 30 and codes[30] == 429
            fake_clock.advance(60.0)
            body = json.dumps({"n": "late"}).encode()
            assert (await post(kit, "fast", body, signed(kit, "hmac", body))).status_code == 200
            # Unauthenticated requests do not use up the route's budget.
            fake_clock.advance(60.0)
            for _ in range(40):
                assert (await post(kit, "fast", body, {})).status_code == 401
            assert len(kit.deliver.sent) == 31
        finally:
            await kit.close()

    run(main())


def test_size_route_and_secret_checks(scope: Scope, fake_clock: FakeClock, tmp_path: Path) -> None:
    async def main() -> None:
        kit = await hook(
            scope,
            fake_clock,
            tmp_path,
            routes={
                "small": route(auth="hmac", deliver_only=True, deliver="local"),
                "off": route(enabled=False),
                "nokey": {"secret": "INDUS_WEBHOOK_MISSING"},
            },
            settings={"max_body_bytes": 64},
        )
        try:
            body = b"{" + b'"a": "' + b"z" * 100 + b'"}'
            assert (await post(kit, "small", body, signed(kit, "hmac", body))).status_code == 413
            ok = b'{"a": 1}'
            assert (await post(kit, "small", ok, signed(kit, "hmac", ok))).status_code == 200
            assert (await post(kit, "nope", ok, signed(kit, "hmac", ok))).status_code == 404
            assert (await post(kit, "off", ok, signed(kit, "hmac", ok))).status_code == 403
            assert (await post(kit, "nokey", ok, signed(kit, "hmac", ok))).status_code == 403
            junk = b"\xff\xfe not json = & = ="
            assert (await post(kit, "small", junk, signed(kit, "hmac", junk))).status_code == 400
        finally:
            await kit.close()

    run(main())


# -- loading routes -----------------------------------------------------------------------------


def test_an_insecure_route_loads_only_on_loopback(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        insecure = {"local": {"insecure": True, "deliver_only": True, "deliver": "local"}}
        with pytest.raises(RouteError, match="loopback"):
            await hook(scope, fake_clock, tmp_path, routes=insecure, settings={"host": "0.0.0.0"})
        with pytest.raises(RouteError, match="no secret"):
            await hook(scope, fake_clock, tmp_path, routes={"bare": {"prompt": "x"}})
        with pytest.raises(RouteError, match="deliver_only"):
            await hook(scope, fake_clock, tmp_path, routes={"d": route(deliver_only=True)})
        with pytest.raises(RouteError):
            await hook(scope, fake_clock, tmp_path, routes={"t": route(promt="typo")})
        kit = await hook(scope, fake_clock, tmp_path, routes=insecure)
        try:
            answer = await post(kit, "local", b'{"x": 1}', {})
            assert answer.status_code == 200 and answer.json()["status"] == "delivered"
        finally:
            await kit.close()

    run(main())


def test_subscribed_routes_are_picked_up_without_a_restart(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await hook(
            scope,
            fake_clock,
            tmp_path,
            routes={
                "both": route(auth="hmac", deliver_only=True, deliver="local", prompt="config")
            },
        )
        try:
            body = b'{"k": "v"}'
            assert (await post(kit, "sub", body, signed(kit, "hmac", body))).status_code == 404
            store = SubscriptionStore(scope)
            store.put("sub", route(auth="hmac", deliver_only=True, deliver="local", prompt="{k}"))
            store.put("both", route(auth="hmac", deliver_only=True, deliver="local", prompt="file"))
            store.put("open", {"insecure": True, "deliver_only": True, "deliver": "local"})
            assert (await post(kit, "sub", body, signed(kit, "hmac", body))).status_code == 200
            body2 = b'{"k": "w"}'
            assert (await post(kit, "both", body2, signed(kit, "hmac", body2))).status_code == 200
            assert [s.text for s in kit.deliver.sent] == ["v", "config"]  # config wins
            assert (await post(kit, "open", body, {})).status_code == 200  # loopback receiver
            store.remove("sub")
            body3 = b'{"k": "x"}'
            assert (await post(kit, "sub", body3, signed(kit, "hmac", body3))).status_code == 404
        finally:
            await kit.close()

    run(main())


# -- filters, delivery --------------------------------------------------------------------------


def test_events_filters_and_direct_delivery(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await hook(
            scope,
            fake_clock,
            tmp_path,
            routes={
                "gl": route(
                    auth="gitlab",
                    events=["Merge Request Hook"],
                    filters=[
                        {"field": "object_attributes.state", "equals": "opened"},
                        {"not": {"field": "object_attributes.draft", "equals": True}},
                    ],
                    deliver_only=True,
                    deliver="slack:C1",
                    prompt="MR !{object_attributes.iid} {object_attributes.title} by "
                    "{user.username}{missing.key}",
                )
            },
        )
        try:
            body = payload("gitlab_merge_request.json")
            headers = signed(kit, "gitlab", body, **{"X-Gitlab-Event": "Merge Request Hook"})
            answer = await post(kit, "gl", body, headers)
            assert answer.status_code == 200 and answer.json()["status"] == "delivered"
            assert kit.deliver.sent[-1].text == "MR !7 Fix the footer links by pine-marten"
            other = signed(kit, "gitlab", body, **{"X-Gitlab-Event": "Push Hook"})
            assert (await post(kit, "gl", body, other)).json()["status"] == "ignored"
            closed = json.loads(body)
            closed["object_attributes"]["state"] = "closed"
            raw = json.dumps(closed).encode()
            headers = signed(kit, "gitlab", raw, **{"X-Gitlab-Event": "Merge Request Hook"})
            assert (await post(kit, "gl", raw, headers)).json()["reason"] == "filter"
            kit.deliver.fail = True
            failing = json.dumps({**json.loads(body), "id": 2}).encode()
            headers = signed(kit, "gitlab", failing, **{"X-Gitlab-Event": "Merge Request Hook"})
            assert (await post(kit, "gl", failing, headers)).status_code == 502
            kit.deliver.fail = False
            assert (await post(kit, "gl", failing, headers)).status_code == 200  # retry taken
            assert kit.server.model_requests == []  # direct delivery never calls the model
        finally:
            await kit.close()

    run(main())


def test_a_silent_reply_is_not_delivered(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await hook(
            scope,
            fake_clock,
            tmp_path,
            text_turn("[SILENT]"),
            routes={"a": route(auth="hmac", deliver="telegram:1")},
        )
        try:
            body = payload("generic_alert.json")
            assert (await post(kit, "a", body, signed(kit, "hmac", body))).status_code == 202
            await kit.receiver.drain()
            assert len(kit.server.model_requests) == 1 and kit.deliver.sent == []
            assert kit.service.attached() == []  # the one-shot session was closed
        finally:
            await kit.close()

    run(main())


# -- coalescing ---------------------------------------------------------------------------------


def test_coalescing_runs_the_newest_event_once_after_the_quiet_window(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await hook(
            scope,
            fake_clock,
            tmp_path,
            text_turn("one turn"),
            text_turn("two"),
            routes={
                "pr": route(
                    auth="hmac",
                    prompt="title={pull_request.title}",
                    coalesce={
                        "key": "pull_request.number",
                        "window_seconds": 30,
                        "max_wait_seconds": 50,
                    },
                )
            },
        )
        try:
            base = json.loads(payload("github_pull_request.json"))
            for n, title in enumerate(["v1", "v2", "v3"]):
                base["pull_request"]["title"] = title
                body = json.dumps({**base, "seq": n}).encode()
                answer = await post(kit, "pr", body, signed(kit, "hmac", body))
                assert answer.json()["status"] == "coalesced"
                await fake_clock.advance_and_settle(10.0)
            assert kit.server.model_requests == []
            await fake_clock.advance_and_settle(30.0)
            await until(lambda: kit.server.model_requests)
            await kit.receiver.drain()
            assert len(kit.server.model_requests) == 1
            prompt = str(kit.server.model_requests[0].body)
            assert "title=v3" in prompt and "3 events" in prompt and "title=v1" not in prompt
            # A steady stream is bounded by max_wait: events every 20 s, released at 50 s.
            for n in range(4):
                base["pull_request"]["title"] = f"w{n}"
                body = json.dumps({**base, "seq": 10 + n}).encode()
                await post(kit, "pr", body, signed(kit, "hmac", body))
                if n < 3:
                    await fake_clock.advance_and_settle(20.0)
                if len(kit.server.model_requests) > 1:
                    break
            await until(lambda: len(kit.server.model_requests) == 2)
            await kit.receiver.drain()
            second = str(kit.server.model_requests[1].body)
            assert "title=w2" in second, second[-600:]
        finally:
            await kit.close()

    run(main())


def test_an_unresolved_coalesce_key_runs_at_once(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await hook(
            scope,
            fake_clock,
            tmp_path,
            text_turn("now"),
            routes={"a": route(auth="hmac", coalesce={"key": "no.such.field"})},
        )
        try:
            body = payload("generic_alert.json")
            answer = await post(kit, "a", body, signed(kit, "hmac", body))
            assert answer.json()["status"] == "accepted"
            await kit.receiver.drain()
            assert len(kit.server.model_requests) == 1
        finally:
            await kit.close()

    run(main())


# -- R02.27: the untrusted tool floor -----------------------------------------------------------


def test_a_webhook_session_has_exactly_the_untrusted_floor(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        registry = ToolRegistry()
        load_builtins(registry)
        kit = await hook(scope, fake_clock, tmp_path, routes={"a": route()}, registry=registry)
        try:
            sid = await kit.desk.open(None)
            names = set(kit.desk.tool_names(sid))
            assert names and names <= set(WEBHOOK_FLOOR)
            assert {"web_search", "web_extract", "vision_analyze", "clarify"} >= names
            assert "terminal" not in names and "write_file" not in names
        finally:
            await kit.close()

    run(main())


def test_a_session_wider_than_the_floor_is_refused(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        registry = ToolRegistry()
        load_builtins(registry)
        kit = await hook(
            scope,
            fake_clock,
            tmp_path,
            text_turn("never"),
            routes={"a": route(auth="hmac")},
            registry=registry,
        )
        wide = make_service(
            scope,
            kit.server,
            tmp_path / "wide",
            kit.store,
            toolsets=["indus-api-server"],
            kinds=(),
            registry=registry,
            platform="webhook",
        )
        kit.desk.service = wide
        kit.desk.toolsets = ["indus-api-server"]
        try:
            body = payload("generic_alert.json")
            assert (await post(kit, "a", body, signed(kit, "hmac", body))).status_code == 202
            await kit.receiver.drain()
            assert kit.server.model_requests == []
        finally:
            await wide.aclose()
            await kit.close()

    run(main())
