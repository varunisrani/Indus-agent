import asyncio

import pytest

from tests.fakes.backend import CommandCall, CommandResult, FakeBackend, UnscriptedCommand
from tests.fakes.platform import Edit, FakePlatform, InboundEvent, SentMessage


def test_platform_records_sends_and_edits() -> None:
    async def main() -> FakePlatform:
        platform = FakePlatform()
        first = await platform.send("chat-1", "working", attachments=("plot.png",))
        second = await platform.send("chat-2", "hi")
        await platform.edit("chat-1", first, "done")
        assert first != second
        with pytest.raises(KeyError):
            await platform.edit("chat-2", first, "wrong chat")
        return platform

    platform = asyncio.run(main())
    assert platform.sent == [
        SentMessage("m1", "chat-1", "working", ("plot.png",)),
        SentMessage("m2", "chat-2", "hi"),
    ]
    assert platform.edits == [Edit("chat-1", "m1", "done")]
    assert platform.texts() == ["done", "hi"]
    assert platform.texts("chat-2") == ["hi"]


def test_platform_replays_inbound_in_order() -> None:
    async def main() -> list[InboundEvent | None]:
        platform = FakePlatform()
        platform.push_inbound(InboundEvent("c", "u1", "hello"), InboundEvent("c", "u2", "again"))
        return [await platform.receive() for _ in range(3)]

    first, second, third = asyncio.run(main())
    assert first is not None and first.text == "hello"
    assert second is not None and second.user_id == "u2"
    assert third is None


def test_backend_answers_by_pattern_and_records_calls() -> None:
    async def main() -> FakeBackend:
        backend = FakeBackend()
        backend.script("git status*", stdout="clean\n", duration=0.2)
        backend.script("false", exit_code=1)
        backend.script("*", exit_code=127, stderr="not found")
        env = {"PATH": "/bin"}
        assert await backend.run(["git", "status", "--short"], cwd="/w", env=env) == (
            CommandResult(0, "clean\n", "", 0.2)
        )
        env["PATH"] = "changed after the call"
        assert (await backend.run(["false"])).exit_code == 1
        assert (await backend.run(["nope", "a b"])).stderr == "not found"
        return backend

    backend = asyncio.run(main())
    assert backend.calls == [
        CommandCall(("git", "status", "--short"), "/w", {"PATH": "/bin"}),
        CommandCall(("false",), None, None),
        CommandCall(("nope", "a b"), None, None),
    ]


def test_backend_unscripted_command_is_an_error() -> None:
    backend = FakeBackend()
    with pytest.raises(UnscriptedCommand, match="ls -la"):
        asyncio.run(backend.run(["ls", "-la"]))
    assert len(backend.calls) == 1
