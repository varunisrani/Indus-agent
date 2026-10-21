"""``pet.*``: the pet methods the desktop and the TUI call (P24-19).

``PET_METHODS`` (the wire models with it, in ``indus.protocol.pets``, re-exported here) lists the
rows of the method catalogue and ``PET_HANDLERS`` maps each name to its handler; ``register(table)``
adds them to a dispatcher table (the daemon's WebSocket and the TUI bridge), and
``PET_DEADLINES`` gives the dispatcher the two long deadlines (the drawing steps).

Handlers take ``(context, params)`` where the context carries the caller's ``Scope``. Store work
runs off the event loop; the drawing steps await the image service directly. A missing pet or a
bad value is ``INVALID_PARAMS`` with the reason; no usable image service or a missed drawing
deadline is ``PROVIDER_UNAVAILABLE``.
"""

from __future__ import annotations

import base64
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol

from indus.foundation.blocking import run_blocking
from indus.foundation.scope import Scope
from indus.protocol.base import ProtocolModel
from indus.protocol.errors import INVALID_PARAMS, PROVIDER_UNAVAILABLE
from indus.protocol.methods import ProtocolError
from indus.protocol.pets import (
    PET_DEADLINES,
    PET_METHODS,
    PetDisableResult,
    PetDraftInfo,
    PetGenerateParams,
    PetGenerateResult,
    PetHatchParams,
    PetInfoParams,
    PetInfoResult,
    PetListResult,
    PetNoParams,
    PetRenameParams,
    PetRow,
    PetSlugParams,
    PetSlugResult,
)
from indus.service import pets
from indus.service.pets import PetError, PetStore, PetTimeout, PetUnavailable

__all__ = [
    "PET_DEADLINES",
    "PET_HANDLERS",
    "PET_METHODS",
    "PetContext",
    "PetDraftInfo",
    "PetGenerateParams",
    "PetGenerateResult",
    "PetHatchParams",
    "PetInfoParams",
    "PetInfoResult",
    "PetListResult",
    "PetRow",
    "PetSlugParams",
    "PetSlugResult",
    "register",
]


class PetContext(Protocol):
    @property
    def scope(self) -> Scope: ...


def _fail(exc: PetError) -> ProtocolError:
    if isinstance(exc, (PetUnavailable, PetTimeout)):
        return ProtocolError(PROVIDER_UNAVAILABLE, str(exc))
    return ProtocolError(INVALID_PARAMS, str(exc))


async def _with_store[T](scope: Scope, fn: Callable[[PetStore], T]) -> T:
    def call() -> T:
        try:
            return fn(PetStore(scope))
        except PetError as exc:
            raise _fail(exc) from exc

    return await run_blocking(call, label="pet rpc")


def _slug_result(store: PetStore, pet: pets.Pet) -> PetSlugResult:
    settings = store.settings()
    return PetSlugResult(
        slug=pet.slug, name=pet.name, shown=settings.enabled and settings.active == pet.slug
    )


async def pet_info(ctx: PetContext, params: PetInfoParams) -> PetInfoResult:
    def fn(store: PetStore) -> PetInfoResult:
        pet = store.shown()
        if pet is None:
            return PetInfoResult(enabled=False)
        payload = pets.sprite_payload(pet, store.settings())
        same = params.known_revision is not None and params.known_revision == payload["revision"]
        return PetInfoResult(
            enabled=True,
            slug=pet.slug,
            name=pet.name,
            scale=payload["scale"],
            revision=payload["revision"],
            unchanged=True if same else None,
            moods=None if same else payload["moods"],
        )

    return await _with_store(ctx.scope, fn)


async def pet_list(ctx: PetContext, params: PetNoParams) -> PetListResult:
    def fn(store: PetStore) -> PetListResult:
        settings = store.settings()
        return PetListResult(
            pets=[
                PetRow(
                    slug=p.slug,
                    name=p.name,
                    shown=settings.enabled and settings.active == p.slug,
                )
                for p in store.list()
            ],
            active=settings.active,
            enabled=settings.enabled,
        )

    return await _with_store(ctx.scope, fn)


async def pet_generate(ctx: PetContext, params: PetGenerateParams) -> PetGenerateResult:
    try:
        made = await pets.generate_drafts(ctx.scope, params.idea, count=params.count)
    except PetError as exc:
        raise _fail(exc) from exc

    def encode() -> list[PetDraftInfo]:
        return [
            PetDraftInfo(
                index=d.index,
                data_uri=f"data:{d.mime};base64,"
                + base64.b64encode(d.path.read_bytes()).decode("ascii"),
            )
            for d in made.drafts
        ]

    return PetGenerateResult(token=made.token, drafts=await run_blocking(encode, label="pet rpc"))


async def pet_hatch(ctx: PetContext, params: PetHatchParams) -> PetSlugResult:
    try:
        pet = await pets.hatch(ctx.scope, params.token, params.pick, params.name)
    except PetError as exc:
        raise _fail(exc) from exc
    return await _with_store(ctx.scope, lambda store: _slug_result(store, pet))


async def pet_select(ctx: PetContext, params: PetSlugParams) -> PetSlugResult:
    return await _with_store(ctx.scope, lambda store: _slug_result(store, store.use(params.slug)))


async def pet_disable(ctx: PetContext, params: PetNoParams) -> PetDisableResult:
    def fn(store: PetStore) -> PetDisableResult:
        store.off()
        return PetDisableResult(enabled=False)

    return await _with_store(ctx.scope, fn)


async def pet_rename(ctx: PetContext, params: PetRenameParams) -> PetSlugResult:
    return await _with_store(
        ctx.scope, lambda store: _slug_result(store, store.rename(params.slug, params.name))
    )


async def pet_remove(ctx: PetContext, params: PetSlugParams) -> PetSlugResult:
    def fn(store: PetStore) -> PetSlugResult:
        pet = store.remove(params.slug)
        return PetSlugResult(slug=pet.slug, name=pet.name, shown=False)

    return await _with_store(ctx.scope, fn)


type _Handler = Callable[[Any, Any], Awaitable[ProtocolModel]]

PET_HANDLERS: Mapping[str, _Handler] = {
    "pet.info": pet_info,
    "pet.list": pet_list,
    "pet.generate": pet_generate,
    "pet.hatch": pet_hatch,
    "pet.select": pet_select,
    "pet.disable": pet_disable,
    "pet.rename": pet_rename,
    "pet.remove": pet_remove,
}


def register(table: Any) -> None:
    """Add every handler to a ``MethodTable`` (the names must be in the method catalogue)."""
    for name, handler in PET_HANDLERS.items():
        table.add(name, handler)
