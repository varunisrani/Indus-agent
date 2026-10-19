"""vision_analyze: loading rules, image checks, crop and downscale, routes, budget (P10-15)."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator, Iterator, Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest

from indus.foundation.errors import ProviderHTTPError
from indus.foundation.scope import Scope
from indus.safety import url_policy
from indus.tools.builtin.media import imaging
from indus.tools.builtin.media import vision as vision_mod
from indus.tools.builtin.media.vision import (
    CHILD_EMBED_CAP,
    EMBED_MAX_SIDE,
    MAX_SOURCE_BYTES,
    EmbedBudget,
    VisionTool,
    embed_target_bytes,
)
from indus.tools.calls import ToolCall, ToolContext
from indus.tools.dispatch import dispatch
from indus.tools.registry import ToolRegistry
from tests.fakes.agent import DictConfig
from tests.fakes.aux_model import FakeAuxModel
from tests.fakes.resolver import FakeResolver
from tests.unit.tools.builtin.media import _images

PUBLIC = "93.184.216.34"


@pytest.fixture(autouse=True)
def _fresh_blocklist() -> Iterator[None]:
    url_policy.clear_blocklist_cache()
    yield
    url_policy.clear_blocklist_cache()


CODECS = ["pure", *(["pillow"] if imaging.pillow() is not None else [])]


@pytest.fixture(params=CODECS)
def codec(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """Run pixel tests on our PNG codec and, when it is installed, on Pillow."""
    if request.param == "pure":
        monkeypatch.setattr(imaging, "pillow", lambda: None)
    return str(request.param)


def configured(scope: Scope, data: Mapping[str, Any] | None = None) -> Scope:
    return scope.with_config(DictConfig(data or {}))


class Web:
    """A fake network behind ``safe_client``: host table for the resolver, handler per path."""

    def __init__(self, hosts: Mapping[str, str] | None = None) -> None:
        self.resolver = FakeResolver({h: [a] for h, a in (hosts or {}).items()})
        self.requests: list[httpx.Request] = []
        self.routes: dict[str, httpx.Response] = {}

    def factory(self, proxy: str | None) -> httpx.AsyncBaseTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            response = self.routes.get(request.url.path)
            return response if response is not None else httpx.Response(404)

        return httpx.MockTransport(handler)

    def tool(self, **kwargs: Any) -> VisionTool:
        return VisionTool(resolver=self.resolver, inner_factory=self.factory, **kwargs)


def run(tool: VisionTool, scope: Scope, **kwargs: Any) -> Any:
    args = {k: kwargs.pop(k) for k in ("image_url", "question", "region") if k in kwargs}
    args.setdefault("question", "What is shown?")
    return asyncio.run(tool.analyze(args, scope=scope, **kwargs))


def embedded(result: Any) -> tuple[bytes, imaging.ImageInfo]:
    assert isinstance(result, dict), result
    assert set(result) == {"multimodal", "content", "text_summary"}
    url = result["content"][1]["image_url"]["url"]
    data = base64.b64decode(url.split(",", 1)[1])
    return data, imaging.inspect(data)


def error_of(result: Any) -> str:
    assert isinstance(result, str), result
    body = json.loads(result)
    assert body.get("success") is False, body
    return str(body["error"])


# -- types and limits ---------------------------------------------------------------------------


def test_png_with_wrong_extension_accepted(tmp_path: Path, scope: Scope) -> None:
    picture = tmp_path / "photo.jpg"
    picture.write_bytes(_images.png(8, 6))
    data, info = embedded(run(VisionTool(), scope, image_url=str(picture), native_vision=True))
    assert info.mime == "image/png" and (info.width, info.height) == (8, 6)
    assert data == picture.read_bytes()  # small enough: sent unchanged


def test_text_named_png_refused(tmp_path: Path, scope: Scope) -> None:
    fake = tmp_path / "chart.png"
    fake.write_text("name,value\nalpha,1\n")
    message = error_of(run(VisionTool(), scope, image_url=str(fake), native_vision=True))
    assert "not a supported image" in message and "text" in message


def test_header_facts_for_each_type() -> None:
    assert imaging.inspect(_images.jpeg_header(640, 480)) == imaging.ImageInfo(
        "image/jpeg", 640, 480
    )
    assert imaging.inspect(_images.gif(30, 20, 3)) == imaging.ImageInfo("image/gif", 30, 20, 3)
    assert imaging.inspect(_images.webp_animated(300, 200, 7)) == imaging.ImageInfo(
        "image/webp", 300, 200, 7
    )
    apng = _images.png(4, 4)
    actl = b"acTL" + (5).to_bytes(4, "big") + (0).to_bytes(4, "big")
    import zlib

    chunk = len(actl[4:]).to_bytes(4, "big") + actl + zlib.crc32(actl).to_bytes(4, "big")
    apng = apng[:33] + chunk + apng[33:]
    assert imaging.inspect(apng).frames == 5


def test_animation_caps(tmp_path: Path, scope: Scope) -> None:
    many = tmp_path / "many.gif"
    many.write_bytes(_images.gif(10, 10, 101))
    message = error_of(run(VisionTool(), scope, image_url=str(many), native_vision=True))
    assert "more than 100 frames" in message
    huge = tmp_path / "huge.gif"
    huge.write_bytes(_images.gif(2000, 2000, 30))  # 120,000,000 pixels in total
    message = error_of(run(VisionTool(), scope, image_url=str(huge), native_vision=True))
    assert "100,000,000" in message


def test_truncated_png_refused(tmp_path: Path, scope: Scope) -> None:
    whole = _images.png(40, 40)
    cut = tmp_path / "cut.png"
    cut.write_bytes(whole[: len(whole) - 30])
    message = error_of(run(VisionTool(), scope, image_url=str(cut), native_vision=True))
    assert "truncated" in message or "damaged" in message


def test_secret_files_and_bad_sources_refused(tmp_path: Path, scope: Scope) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("TOKEN=x\n")
    assert "secret-bearing" in error_of(
        run(VisionTool(), scope, image_url=str(env_file), native_vision=True)
    )
    assert "no file at" in error_of(
        run(VisionTool(), scope, image_url=str(tmp_path / "missing.png"), native_vision=True)
    )
    assert "unsupported source" in error_of(
        run(VisionTool(), scope, image_url="ftp://host/a.png", native_vision=True)
    )
    assert "base64" in error_of(
        run(VisionTool(), scope, image_url="data:image/png,abc", native_vision=True)
    )
    assert error_of(run(VisionTool(), scope, image_url="  ")) == "image_url is required"


def test_data_url_and_file_uri(tmp_path: Path, scope: Scope) -> None:
    raw = _images.png(5, 5)
    url = "data:image/png;base64," + base64.b64encode(raw).decode()
    data, _ = embedded(run(VisionTool(), scope, image_url=url, native_vision=True))
    assert data == raw
    picture = tmp_path / "p.png"
    picture.write_bytes(raw)
    data, _ = embedded(run(VisionTool(), scope, image_url=f"file://{picture}", native_vision=True))
    assert data == raw


# -- network ------------------------------------------------------------------------------------


def test_private_url_refused_before_any_request(scope: Scope) -> None:
    web = Web({"intranet.example": "10.0.0.7"})
    message = error_of(
        run(web.tool(), scope, image_url="http://intranet.example/a.png", native_vision=True)
    )
    assert "refused" in message
    assert web.requests == []


def test_redirect_to_private_address_refused(scope: Scope) -> None:
    web = Web({"cdn.example": PUBLIC, "inside.example": "192.168.1.9"})
    web.routes["/a.png"] = httpx.Response(302, headers={"location": "http://inside.example/b.png"})
    message = error_of(
        run(web.tool(), scope, image_url="https://cdn.example/a.png", native_vision=True)
    )
    assert "refused" in message
    assert [r.headers["host"] for r in web.requests] == ["cdn.example"]


def test_public_url_downloaded(scope: Scope) -> None:
    web = Web({"cdn.example": PUBLIC})
    raw = _images.png(12, 9)
    web.routes["/pic"] = httpx.Response(200, content=raw)
    data, _ = embedded(
        run(web.tool(), scope, image_url="https://cdn.example/pic", native_vision=True)
    )
    assert data == raw


def test_download_cap(scope: Scope) -> None:
    web = Web({"cdn.example": PUBLIC})
    web.routes["/declared"] = httpx.Response(
        200, headers={"content-length": str(MAX_SOURCE_BYTES + 1)}, content=b"x"
    )
    piece = b"\0" * (1024 * 1024)

    async def body() -> AsyncIterator[bytes]:
        for _ in range(51):
            yield piece

    web.routes["/streamed"] = httpx.Response(200, content=body())
    for path in ("/declared", "/streamed"):
        message = error_of(
            run(web.tool(), scope, image_url=f"https://cdn.example{path}", native_vision=True)
        )
        assert "50 MB" in message


# -- pixels -------------------------------------------------------------------------------------


def test_native_downscales_4000px(codec: str, scope: Scope, tmp_path: Path) -> None:
    wide = tmp_path / "wide.png"
    wide.write_bytes(_images.png(4000, 10))
    result = run(VisionTool(), scope, image_url=str(wide), native_vision=True)
    _, info = embedded(result)
    assert info.width == EMBED_MAX_SIDE
    assert info.height in (3, 4)
    assert "shrunk from 4000x10" in result["content"][0]["text"]


def test_region_crop_pixel_exact(codec: str, scope: Scope, tmp_path: Path) -> None:
    def colour(x: int, y: int) -> tuple[int, int, int]:
        return (x * 3 % 256, y * 5 % 256, (x * y) % 256)

    picture = tmp_path / "grid.png"
    picture.write_bytes(_images.png(64, 48, colour))
    result = run(
        VisionTool(), scope, image_url=str(picture), region=[10, 5, 30, 25], native_vision=True
    )
    data, info = embedded(result)
    assert (info.width, info.height) == (20, 20)
    raster = imaging.decode_png(data)
    for y in range(20):
        for x in range(20):
            assert raster.pixel(x, y)[:3] == colour(x + 10, y + 5)
    assert "(10, 5)" in result["content"][0]["text"]


def test_region_clamped_and_zero_area(scope: Scope, tmp_path: Path) -> None:
    picture = tmp_path / "small.png"
    picture.write_bytes(_images.png(30, 20))
    data, info = embedded(
        run(
            VisionTool(), scope, image_url=str(picture), region=[20, 10, 99, 99], native_vision=True
        )
    )
    assert (info.width, info.height) == (10, 10)
    message = error_of(
        run(VisionTool(), scope, image_url=str(picture), region=[25, 5, 25, 15], native_vision=True)
    )
    assert "30x20 px" in message
    message = error_of(
        run(VisionTool(), scope, image_url=str(picture), region=[1, 2, 3], native_vision=True)
    )
    assert "four numbers" in message


def test_png_decoder_reads_every_filter_and_mode() -> None:
    for channels in (1, 2, 3, 4):
        raw = _images.png(9, 7, lambda x, y: (x * 20, y * 30, x + y, 200), channels=channels)
        raster = imaging.decode_png(raw)
        assert raster.channels == channels
        assert raster.pixel(4, 3) == (80, 90, 7, 200)[:channels]
        assert imaging.decode_png(imaging.encode_png(raster)) == raster


def test_embed_target_clamped(scope: Scope) -> None:
    assert embed_target_bytes(configured(scope)) == 256 * 1024
    assert embed_target_bytes(configured(scope, {"vision": {"embed_target_kb": 1}})) == 64 * 1024
    big = configured(scope, {"vision": {"embed_target_kb": 10**6}})
    assert embed_target_bytes(big) == 4096 * 1024


def test_embed_target_drives_png_size(codec: str, scope: Scope, tmp_path: Path) -> None:
    noisy = tmp_path / "noise.png"
    noisy.write_bytes(
        _images.png(600, 600, lambda x, y: ((x * 7919 + y * 104729) % 251, (x * y) % 253, 17))
    )
    target = configured(scope, {"vision": {"embed_target_kb": 64}})
    data, info = embedded(run(VisionTool(), target, image_url=str(noisy), native_vision=True))
    assert imaging.b64_len(len(data)) <= 64 * 1024
    assert info.width < 600


def test_jpeg_too_big_without_pillow_goes_to_text(
    monkeypatch: pytest.MonkeyPatch, scope: Scope, tmp_path: Path
) -> None:
    monkeypatch.setattr(imaging, "pillow", lambda: None)
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(_images.jpeg_header(5000, 3000))
    aux = FakeAuxModel("A wide landscape.")
    body = json.loads(run(VisionTool(), scope, image_url=str(photo), native_vision=True, aux=aux))
    assert body["analysis"] == "A wide landscape."
    assert "vision" in body["note"] and "Pillow" in body["note"]


# -- routes and the child budget ----------------------------------------------------------------


def test_non_vision_route_asks_the_side_model(scope: Scope, tmp_path: Path) -> None:
    picture = tmp_path / "chart.png"
    picture.write_bytes(_images.png(16, 16))
    aux = FakeAuxModel("A bar chart with two bars.")
    body = json.loads(
        run(VisionTool(), scope, image_url=str(picture), question="Which bar is taller?", aux=aux)
    )
    assert body == {"success": True, "analysis": "A bar chart with two bars."}
    (call,) = aux.calls
    assert call.task == "vision"
    text_part, image_part = call.messages[0]["content"]
    assert text_part == {"type": "text", "text": "Which bar is taller?"}
    assert image_part["image_url"]["url"].startswith("data:image/png;base64,")


def test_side_model_errors_become_hints(scope: Scope, tmp_path: Path) -> None:
    picture = tmp_path / "p.png"
    picture.write_bytes(_images.png(4, 4))
    billing = FakeAuxModel(ProviderHTTPError("payment required", status=402))
    assert "billing" in error_of(run(VisionTool(), scope, image_url=str(picture), aux=billing))
    blind = FakeAuxModel(RuntimeError("model does not support image input"))
    assert "cannot read images" in error_of(
        run(VisionTool(), scope, image_url=str(picture), aux=blind)
    )
    message = error_of(run(VisionTool(), scope, image_url=str(picture)))
    assert "auxiliary.vision" in message


def test_fourth_embed_in_child_goes_to_text(scope: Scope, tmp_path: Path) -> None:
    picture = tmp_path / "screen.png"
    picture.write_bytes(_images.png(40, 30))
    tool = VisionTool()
    aux = FakeAuxModel("A login form.")
    regions: list[dict[str, Any]] = [{}, {"region": [0, 0, 10, 10]}, {}, {"region": [5, 5, 20, 20]}]
    child = {"native_vision": True, "subagent": True, "session_id": "child-1", "aux": aux}
    results = [run(tool, scope, image_url=str(picture), **extra, **child) for extra in regions]
    assert all(isinstance(r, dict) for r in results[:CHILD_EMBED_CAP])
    body = json.loads(results[3])
    assert body["analysis"] == "A login form." and "3 times" in body["note"]
    assert len(aux.calls) == 1
    without_aux = {k: v for k, v in child.items() if k != "aux"}
    message = error_of(run(tool, scope, image_url=str(picture), **without_aux))
    assert "3 times" in message and "already seen" in message
    # Another session, and the parent (not a child), are not limited by this count.
    other = run(
        tool, scope, image_url=str(picture), native_vision=True, subagent=True, session_id="c2"
    )
    assert isinstance(other, dict)
    for _ in range(5):
        parent = run(tool, scope, image_url=str(picture), native_vision=True, session_id="child-1")
        assert isinstance(parent, dict)


def test_failed_embed_gives_the_slot_back(
    monkeypatch: pytest.MonkeyPatch, scope: Scope, tmp_path: Path
) -> None:
    picture = tmp_path / "p.png"
    picture.write_bytes(_images.png(10, 10))
    tool = VisionTool()

    def broken(*_: Any, **__: Any) -> Any:
        raise imaging.BadImage("cannot shrink")

    monkeypatch.setattr(vision_mod.imaging, "fit", broken)
    result = run(
        tool,
        scope,
        image_url=str(picture),
        native_vision=True,
        subagent=True,
        session_id="s",
        aux=FakeAuxModel("ok"),
    )
    assert json.loads(result)["success"] is False or "analysis" in json.loads(result)
    assert tool.budget.count("s", f"file:{picture.resolve()}") == 0


def test_budget_is_bounded_and_releases() -> None:
    budget = EmbedBudget(cap=2, max_keys=3)
    assert budget.reserve("s", "a") and budget.reserve("s", "a")
    assert not budget.reserve("s", "a")
    budget.release("s", "a")
    assert budget.reserve("s", "a")
    for key in ("b", "c", "d"):
        budget.reserve("s", key)
    assert len(budget) == 3 and budget.count("s", "a") == 0  # oldest forgotten


def test_budget_parallel_reservations_never_exceed_cap() -> None:
    budget = EmbedBudget(cap=3)

    async def many() -> list[bool]:
        async def one() -> bool:
            return budget.reserve("s", "img")

        return list(await asyncio.gather(*(one() for _ in range(10))))

    assert sum(asyncio.run(many())) == 3


def test_dispatch_through_the_registry(scope: Scope, tmp_path: Path) -> None:
    picture = tmp_path / "p.png"
    picture.write_bytes(_images.png(6, 6))
    registry = ToolRegistry()
    VisionTool().register(registry)
    entry = registry.get("vision_analyze")
    assert entry is not None and entry.traits.read_only
    context = ToolContext(scope=scope, session_id="s1", extras={"native_vision": True})
    call = ToolCall("vision_analyze", {"image_url": str(picture), "question": "?"}, "c1")
    result = asyncio.run(dispatch(call, context, registry=registry))
    assert isinstance(result.content, Mapping) and result.content["multimodal"] is True
