"""P24-19: the `pet.*` handlers over a temp profile with a fake image service."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from indus.foundation.scope import Scope
from indus.protocol.errors import INVALID_PARAMS, PROVIDER_UNAVAILABLE
from indus.protocol.methods import ProtocolError
from indus.service import pets, pets_rpc
from indus.service.pets_rpc import (
    PetGenerateParams,
    PetHatchParams,
    PetInfoParams,
    PetNoParams,
    PetRenameParams,
    PetSlugParams,
)
from indus.tools.builtin.media.providers import ImageCapabilities
from tests.fakes.media_providers import FakeImageProvider


@dataclass
class Ctx:
    scope: Scope


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeImageProvider:
    provider = FakeImageProvider(capabilities=ImageCapabilities(edit=True))
    monkeypatch.setattr(pets, "image_provider", lambda scope: provider)
    return provider


def test_rows_handlers_and_deadlines_line_up() -> None:
    names = [row[0] for row in pets_rpc.PET_METHODS]
    assert names == list(pets_rpc.PET_HANDLERS)
    assert all(name.startswith("pet.") for name in names)
    assert pets_rpc.PET_DEADLINES["pet.generate"] >= 420.0
    assert pets_rpc.PET_DEADLINES["pet.hatch"] >= 3_600.0

    added: list[str] = []

    class Table:
        def add(self, name: str, handler: object) -> None:
            added.append(name)

    pets_rpc.register(Table())
    assert added == names


def test_generate_hatch_select_info_round_trip(scope: Scope, fake: FakeImageProvider) -> None:
    ctx = Ctx(scope)

    async def go() -> None:
        made = await pets_rpc.pet_generate(ctx, PetGenerateParams(idea="a round frog", count=2))
        assert [d.index for d in made.drafts] == [0, 1]
        assert made.drafts[0].data_uri.startswith("data:image/png;base64,")
        hatched = await pets_rpc.pet_hatch(
            ctx, PetHatchParams(token=made.token, pick=0, name="Ribbit")
        )
        assert hatched.slug == "ribbit" and not hatched.shown
        off = await pets_rpc.pet_info(ctx, PetInfoParams())
        assert off.enabled is False and off.moods is None
        chosen = await pets_rpc.pet_select(ctx, PetSlugParams(slug="ribbit"))
        assert chosen.shown
        info = await pets_rpc.pet_info(ctx, PetInfoParams())
        assert info.enabled and info.name == "Ribbit" and info.moods is not None
        assert set(info.moods) == set(pets.MOODS)
        cached = await pets_rpc.pet_info(ctx, PetInfoParams(known_revision=info.revision))
        assert cached.unchanged is True and cached.moods is None
        listed = await pets_rpc.pet_list(ctx, PetNoParams())
        assert listed.active == "ribbit" and listed.enabled and listed.pets[0].shown
        renamed = await pets_rpc.pet_rename(ctx, PetRenameParams(slug="ribbit", name="Croak"))
        assert renamed.slug == "croak" and renamed.shown
        assert (await pets_rpc.pet_disable(ctx, PetNoParams())).enabled is False
        removed = await pets_rpc.pet_remove(ctx, PetSlugParams(slug="croak"))
        assert removed.slug == "croak"
        assert (await pets_rpc.pet_list(ctx, PetNoParams())).pets == []

    asyncio.run(go())


def test_errors_map_to_protocol_codes(scope: Scope, fake: FakeImageProvider) -> None:
    ctx = Ctx(scope)
    with pytest.raises(ProtocolError) as missing:
        asyncio.run(pets_rpc.pet_select(ctx, PetSlugParams(slug="nobody")))
    assert missing.value.error is INVALID_PARAMS

    fake.hold = asyncio.Event()

    async def slow(*args: object, **kw: object) -> pets.DraftSet:
        raise pets.PetTimeout("no answer within 420 s")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(pets, "generate_drafts", slow)
        with pytest.raises(ProtocolError) as late:
            asyncio.run(pets_rpc.pet_generate(ctx, PetGenerateParams(idea="x")))
    assert late.value.error is PROVIDER_UNAVAILABLE


def test_params_refuse_unknown_keys_and_bad_counts() -> None:
    with pytest.raises(ValueError):
        PetGenerateParams.model_validate({"idea": "x", "count": 9})
    with pytest.raises(ValueError):
        PetSlugParams.model_validate({"slug": "x", "extra": 1})
