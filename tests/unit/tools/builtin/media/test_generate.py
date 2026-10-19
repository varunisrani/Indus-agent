"""Image and video generation over the provider registries (P10-16)."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from indus.foundation.scope import EnvView, Scope
from indus.safety import url_policy
from indus.tools.builtin.media.generate import GenerationTools
from indus.tools.builtin.media.providers import (
    ImageCapabilities,
    ImageProvider,
    MediaProviders,
    MediaResult,
    OpenAIImages,
    ProviderFailed,
    ProviderRegistry,
    VideoCapabilities,
    VideoProvider,
)
from indus.tools.definitions import definitions
from indus.tools.registry import ToolRegistry
from tests.fakes.agent import DictConfig
from tests.fakes.clock import checkpoint
from tests.fakes.media_providers import PNG_BYTES, FakeImageProvider, FakeVideoProvider
from tests.fakes.resolver import FakeResolver
from tests.fakes.secret_store import MemorySecretStore

VIDEO_NAMES = {"video_generate", "video_edit", "video_extend"}


@pytest.fixture(autouse=True)
def _fresh_blocklist() -> Iterator[None]:
    url_policy.clear_blocklist_cache()
    yield
    url_policy.clear_blocklist_cache()


def providers(
    images: list[ImageProvider] | None = None, videos: list[VideoProvider] | None = None
) -> MediaProviders:
    return MediaProviders(
        ProviderRegistry[ImageProvider]("image", images or []),
        ProviderRegistry[VideoProvider]("video", videos or []),
    )


def configured(scope: Scope, data: Mapping[str, Any] | None = None) -> Scope:
    """The test profile with ``data`` as its config and no inherited environment (no keys)."""
    return scope.with_config(DictConfig(data or {})).with_env(EnvView({}))


def tools_with(
    *images: ImageProvider, videos: list[VideoProvider] | None = None
) -> GenerationTools:
    return GenerationTools(providers(list(images), videos), resolver=FakeResolver())


def body_of(text: str) -> dict[str, Any]:
    return json.loads(text)


def image(tools: GenerationTools, scope: Scope, **args: Any) -> dict[str, Any]:
    args.setdefault("prompt", "a lighthouse at dusk")
    return body_of(asyncio.run(tools.image_generate(args, scope=scope)))


# -- image_generate -----------------------------------------------------------------------------


def test_image_saved_under_cache_media(scope: Scope) -> None:
    fake = FakeImageProvider()
    result = image(tools_with(fake), configured(scope))
    assert result["success"] is True and result["provider"] == "fake"
    saved = Path(result["image"])
    assert saved.is_absolute() and saved.read_bytes() == PNG_BYTES
    assert saved.parent == scope.home / "cache" / "media" and saved.suffix == ".png"
    assert "sandbox_path" not in result


def test_agent_model_ignored(scope: Scope) -> None:
    fake = FakeImageProvider()
    tools = tools_with(fake)
    image(tools, configured(scope), model="the-agents-choice")
    image(tools, configured(scope, {"image_gen": {"model": "user-pick"}}), model="other")
    assert [r.model for r in fake.requests] == ["fake-model-1", "user-pick"]


def test_five_parallel_calls_at_most_four_in_flight(scope: Scope) -> None:
    async def scenario(limit_config: Mapping[str, Any], expected: int) -> list[dict[str, Any]]:
        fake = FakeImageProvider(hold=asyncio.Event())
        tools = tools_with(fake)
        target = configured(scope, limit_config)
        calls = [
            asyncio.ensure_future(tools.image_generate({"prompt": f"p{i}"}, scope=target))
            for i in range(5)
        ]
        for _ in range(50):
            await checkpoint()
        assert fake.in_flight == expected
        assert fake.hold is not None
        fake.hold.set()
        done = [body_of(t) for t in await asyncio.gather(*calls)]
        assert fake.peak == expected
        return done

    results = asyncio.run(scenario({}, 4))
    assert all(r["success"] for r in results)
    assert len({r["image"] for r in results}) == 5
    asyncio.run(scenario({"image_gen": {"max_parallel_requests": 2}}, 2))


def test_aspect_ratio_and_edit_inputs(scope: Scope, tmp_path: Path) -> None:
    fake = FakeImageProvider(capabilities=ImageCapabilities(edit=True, max_reference_images=2))
    tools = tools_with(fake)
    source = tmp_path / "in.png"
    source.write_bytes(PNG_BYTES)
    data_url = "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode()
    result = image(
        tools,
        configured(scope),
        aspect_ratio="portrait",
        image_url=str(source),
        reference_image_urls=[data_url],
    )
    assert result["success"], result
    request = fake.requests[0]
    assert request.aspect_ratio == "portrait"
    assert request.image is not None and request.image.data == PNG_BYTES
    assert [r.mime for r in request.references] == ["image/png"]
    image(tools, configured(scope), aspect_ratio="panorama")
    assert fake.requests[1].aspect_ratio == "landscape"


def test_edit_and_reference_limits(scope: Scope, tmp_path: Path) -> None:
    plain = FakeImageProvider()
    result = image(tools_with(plain), configured(scope), image_url="https://x.example/a.png")
    assert result["error_type"] == "unsupported" and "image_url" in result["error"]
    limited = FakeImageProvider(capabilities=ImageCapabilities(edit=True, max_reference_images=1))
    result = image(tools_with(limited), configured(scope), reference_image_urls=["a.png", "b.png"])
    assert result["error_type"] == "unsupported" and "at most 1" in result["error"]
    assert plain.requests == [] and limited.requests == []


def test_input_images_follow_the_source_rules(scope: Scope, tmp_path: Path) -> None:
    fake = FakeImageProvider(capabilities=ImageCapabilities(edit=True))
    tools = GenerationTools(
        providers([fake]), resolver=FakeResolver({"intranet.example": ["10.1.2.3"]})
    )
    result = image(tools, configured(scope), image_url="http://intranet.example/x.png")
    assert result["error_type"] == "bad_input" and "refused" in result["error"]
    secret_file = tmp_path / ".env"
    secret_file.write_text("A=1\n")
    result = image(tools, configured(scope), image_url=str(secret_file))
    assert result["error_type"] == "bad_input"
    text = tmp_path / "notes.png"
    text.write_text("hello")
    result = image(tools, configured(scope), image_url=str(text))
    assert result["error_type"] == "bad_input" and "not a supported image" in result["error"]
    assert fake.requests == []


def test_provider_errors_and_results(scope: Scope, tmp_path: Path) -> None:
    def broken(_: Any) -> Any:
        raise ProviderFailed("quota used up")

    result = image(tools_with(FakeImageProvider(result=broken)), configured(scope))
    assert result["error_type"] == "provider_error" and "quota used up" in result["error"]
    wrong = FakeImageProvider(result=lambda _: {"url": "https://x"})
    assert image(tools_with(wrong), configured(scope))["error_type"] == "provider_contract"

    url = FakeImageProvider(result=lambda _: MediaResult(url="https://cdn.example/out.png"))
    assert image(tools_with(url), configured(scope))["image"] == "https://cdn.example/out.png"

    elsewhere = tmp_path / "made.webp"
    elsewhere.write_bytes(b"RIFF\x00\x00\x00\x00WEBPVP8 ")
    moved = FakeImageProvider(result=lambda _: MediaResult(path=elsewhere))
    saved = Path(image(tools_with(moved), configured(scope))["image"])
    assert saved.parent == scope.home / "cache" / "media" and saved.suffix == ".webp"
    assert saved.read_bytes() == elsewhere.read_bytes()


def test_no_provider_and_not_ready(scope: Scope) -> None:
    result = image(tools_with(), configured(scope))
    assert result["error_type"] == "no_provider"
    result = image(
        tools_with(FakeImageProvider()), configured(scope, {"image_gen": {"provider": "x"}})
    )
    assert result["error_type"] == "no_provider" and "fake" in result["error"]
    result = image(tools_with(FakeImageProvider(ready=False)), configured(scope))
    assert result["error_type"] == "not_ready" and "FAKE_IMAGE_KEY" in result["error"]
    assert image(tools_with(FakeImageProvider()), configured(scope), prompt=" ")["error_type"] == (
        "bad_arguments"
    )


def test_docker_sandbox_path(scope: Scope) -> None:
    result = image(
        tools_with(FakeImageProvider()), configured(scope, {"terminal": {"backend": "docker"}})
    )
    name = Path(result["image"]).name
    assert result["sandbox_path"] == f"/root/.indus/cache/media/{name}"


def test_image_schema_follows_capabilities(scope: Scope) -> None:
    registry = ToolRegistry()
    plain = FakeImageProvider()
    tools_with(plain).register(registry)
    (entry,) = definitions(registry, ["image_generate"], scope=configured(scope))
    params = entry["function"]["parameters"]["properties"]
    assert "image_url" not in params and "reference_image_urls" not in params
    assert "Fake images" in entry["function"]["description"]

    registry = ToolRegistry()
    editor = FakeImageProvider(capabilities=ImageCapabilities(edit=True, max_reference_images=3))
    tools_with(editor).register(registry)
    (entry,) = definitions(registry, ["image_generate"], scope=configured(scope))
    params = entry["function"]["parameters"]["properties"]
    assert params["reference_image_urls"]["maxItems"] == 3 and "image_url" in params
    assert "up to 3 reference images" in entry["function"]["description"]


def test_image_tool_hidden_when_provider_not_ready(scope: Scope) -> None:
    registry = ToolRegistry()
    tools_with(FakeImageProvider(ready=False)).register(registry)
    assert definitions(registry, ["image_generate"], scope=configured(scope)) == []


# -- video tools --------------------------------------------------------------------------------


def test_no_video_provider_no_video_tools(scope: Scope) -> None:
    registry = ToolRegistry()
    tools_with(FakeImageProvider()).register(registry)
    assert "image_generate" in registry.names()
    assert VIDEO_NAMES.isdisjoint(registry.names())


def test_edit_capable_provider_brings_video_edit(scope: Scope) -> None:
    registry = ToolRegistry()
    tools = tools_with(FakeImageProvider())
    tools.register(registry)
    caps = VideoCapabilities(durations=(5, 10), resolutions=("720p", "1080p"), edit=True)
    tools.providers.video_providers.register(FakeVideoProvider(capabilities=caps))
    assert {"video_generate", "video_edit"} <= set(registry.names())
    assert "video_extend" not in registry.names()
    target = configured(scope)
    built = {
        e["function"]["name"]: e["function"]
        for e in definitions(registry, sorted(VIDEO_NAMES), scope=target)
    }
    assert set(built) == {"video_generate", "video_edit"}
    assert "Durations: 5, 10 seconds" in built["video_generate"]["description"]
    assert built["video_generate"]["parameters"]["properties"]["duration"]["enum"] == [5, 10]
    assert "Change an existing video" in built["video_edit"]["description"]
    assert built["video_edit"]["parameters"]["required"] == ["video_url", "prompt"]
    tools.providers.video_providers.unregister("fakevideo")
    assert VIDEO_NAMES.isdisjoint(registry.names())


def test_video_probe_follows_the_active_provider(scope: Scope) -> None:
    registry = ToolRegistry()
    editor = FakeVideoProvider(name="editor", capabilities=VideoCapabilities(edit=True))
    plain = FakeVideoProvider(name="plain")
    tools_with(videos=[editor, plain]).register(registry)
    assert "video_edit" in registry.names()
    pinned = configured(scope, {"media": {"video_provider": "plain"}})
    names = {
        e["function"]["name"] for e in definitions(registry, sorted(VIDEO_NAMES), scope=pinned)
    }
    assert names == {"video_generate"}


def test_video_generate_request(scope: Scope, tmp_path: Path) -> None:
    caps = VideoCapabilities(durations=(5, 10), resolutions=("720p", "1080p"), image_input=True)
    fake = FakeVideoProvider(capabilities=caps)
    tools = tools_with(videos=[fake])
    still = tmp_path / "still.png"
    still.write_bytes(PNG_BYTES)
    target = configured(scope, {"media": {"video_model": "user-video"}})
    args = {"prompt": "waves", "duration": 8, "resolution": "4k", "image_url": str(still)}
    args["model"] = "agent-pick"
    result = body_of(asyncio.run(tools.video("generate", args, scope=target)))
    assert result["success"] and Path(result["video"]).suffix == ".mp4"
    request = fake.requests[0]
    assert (request.duration, request.resolution, request.aspect_ratio) == (10, "720p", "16:9")
    assert request.model == "user-video" and request.image is not None


def test_video_edit_checks_its_input(scope: Scope) -> None:
    fake = FakeVideoProvider(capabilities=VideoCapabilities(edit=True))
    tools = GenerationTools(
        providers(videos=[fake]),
        resolver=FakeResolver({"cdn.example": ["93.184.216.34"], "lan.example": ["10.0.0.2"]}),
    )
    ok = body_of(
        asyncio.run(
            tools.video(
                "edit",
                {"prompt": "add rain", "video_url": "https://cdn.example/v.mp4"},
                scope=scope,
            )
        )
    )
    assert ok["success"] and fake.requests[0].video is not None
    assert fake.requests[0].video.url == "https://cdn.example/v.mp4"
    refused = body_of(
        asyncio.run(
            tools.video("edit", {"prompt": "x", "video_url": "http://lan.example/v"}, scope=scope)
        )
    )
    assert refused["error_type"] == "bad_input"
    unsupported = body_of(
        asyncio.run(tools.video("extend", {"prompt": "x", "video_url": "a.mp4"}, scope=scope))
    )
    assert unsupported["error_type"] == "unsupported"


# -- the built-in OpenAI-compatible images provider ---------------------------------------------


def _keyed(scope: Scope) -> Scope:
    fake_key = "test-" + "image-key"
    return configured(scope).with_secrets(MemorySecretStore({"OPENAI_API_KEY": fake_key}))


@respx.mock
def test_openai_images_generation(scope: Scope) -> None:
    route = respx.post("https://api.openai.com/v1/images/generations").mock(
        return_value=httpx.Response(
            200, json={"data": [{"b64_json": base64.b64encode(PNG_BYTES).decode()}]}
        )
    )
    tools = GenerationTools(providers([OpenAIImages()]), resolver=FakeResolver())
    result = image(tools, _keyed(scope), aspect_ratio="landscape")
    assert result["success"] and Path(result["image"]).read_bytes() == PNG_BYTES
    sent = route.calls.last.request
    assert sent.headers["authorization"] == "Bearer test-image-key"
    assert json.loads(sent.content) == {
        "model": "gpt-image-1",
        "prompt": "a lighthouse at dusk",
        "size": "1536x1024",
        "n": 1,
    }


@respx.mock
def test_openai_images_edit_and_errors(scope: Scope, tmp_path: Path) -> None:
    route = respx.post("https://images.local/v1/images/edits").mock(
        return_value=httpx.Response(200, json={"data": [{"url": "https://cdn.example/r.png"}]})
    )
    source = tmp_path / "in.png"
    source.write_bytes(PNG_BYTES)
    target = _keyed(scope).with_config(
        DictConfig({"image_gen": {"base_url": "https://images.local/v1"}})
    )
    tools = GenerationTools(providers([OpenAIImages()]), resolver=FakeResolver())
    result = image(tools, target, image_url=str(source))
    assert result["image"] == "https://cdn.example/r.png"
    sent = route.calls.last.request
    assert b'name="image[]"' in sent.content and PNG_BYTES in sent.content

    respx.post("https://api.openai.com/v1/images/generations").mock(
        return_value=httpx.Response(400, json={"error": {"message": "prompt rejected"}})
    )
    failed = image(tools, _keyed(scope))
    assert failed["error_type"] == "provider_error" and "prompt rejected" in failed["error"]
    unkeyed = image(tools, configured(scope))
    assert unkeyed["error_type"] == "not_ready" and "OPENAI_API_KEY" in unkeyed["error"]
