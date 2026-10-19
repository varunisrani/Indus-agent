# pyright: strict
"""Registry rows of the SDK sandbox backends: ``modal``, ``daytona``, ``vercel_sandbox`` (P21-12).

Each row's factory builds the backend through its ``open_*`` entry point with the shared ports
(``sdk_ports.VendorSupport``, ``sdk_ports.ProfileRecords``). The probe answers without the network
and without importing a vendor SDK: the extra's distribution is looked up in the installed
package metadata, then the backend's own ``requirement_reason`` checks the settings and that its
credentials are present. A persistent sandbox serves the whole profile (one environment key, task
key ``shared``), as for Docker and Singularity.

Cleanup of an SDK sandbox is a sync-back (one bulk download, ``BULK_FILE_DEADLINE_S``), a snapshot
(``SNAPSHOT_DEADLINE_S``) and a stop or terminate (``STOP_DEADLINE_S``) in a row, so the rows
carry ``SDK_CLEANUP_BOUND_S`` instead of the registry's default bound.
"""

from __future__ import annotations

import importlib.metadata
from collections.abc import Callable
from typing import TYPE_CHECKING

from indus.backends.contract import BackendFlags, Environment
from indus.backends.docker import persistent_per_profile
from indus.backends.sdk_call import BULK_FILE_DEADLINE_S, SNAPSHOT_DEADLINE_S, STOP_DEADLINE_S
from indus.foundation.scope import Scope

if TYPE_CHECKING:
    from indus.backends.registry import BackendEntry, CreateRequest

__all__ = [
    "DAYTONA_ROW_FLAGS",
    "MODAL_ROW_FLAGS",
    "SDK_CLEANUP_BOUND_S",
    "SHARED_TASK",
    "VERCEL_ROW_FLAGS",
    "daytona_entry",
    "daytona_probe",
    "extra_missing",
    "modal_entry",
    "modal_probe",
    "sdk_entries",
    "task_key_for",
    "vercel_entry",
    "vercel_probe",
]

SDK_CLEANUP_BOUND_S = BULK_FILE_DEADLINE_S + SNAPSHOT_DEADLINE_S + STOP_DEADLINE_S
SHARED_TASK = "shared"

# Row flags (each module's own flags, repeated here so listing the rows imports no backend; a
# test keeps them equal).
MODAL_ROW_FLAGS = BackendFlags(
    isolated=True, container_paths=True, stdin_mode="pipe", host_local=False
)
DAYTONA_ROW_FLAGS = BackendFlags(
    isolated=True, container_paths=True, stdin_mode="payload", host_local=False
)
VERCEL_ROW_FLAGS = BackendFlags(
    isolated=True, container_paths=True, stdin_mode="payload", host_local=False
)

# backend name -> (distribution that provides the SDK, extra that installs it)
_EXTRAS: dict[str, tuple[str, str]] = {
    "modal": ("modal", "modal"),
    "daytona": ("daytona", "daytona"),
    "vercel_sandbox": ("vercel-sandbox", "vercel"),
}

type _Lookup = Callable[[str], object]


def extra_missing(backend: str, *, lookup: _Lookup | None = None) -> str | None:
    """Why ``backend``'s SDK is not installed (naming the extra), or None. Reads package
    metadata only; the SDK itself is never imported."""
    dist, extra = _EXTRAS[backend]
    find: _Lookup = lookup if lookup is not None else importlib.metadata.distribution
    try:
        find(dist)
    except importlib.metadata.PackageNotFoundError:
        return (
            f"the {backend} backend needs the {dist} SDK, and indus was installed without the "
            f"`{extra}` extra (install indus-agent[{extra}])"
        )
    return None


def task_key_for(request: CreateRequest) -> str:
    """The records/sandbox key: ``shared`` for a profile-wide sandbox, else the env key's id."""
    return SHARED_TASK if request.key.kind == "profile" else request.key.ident


def modal_probe(scope: Scope) -> str | None:
    from indus.backends.modal.lifecycle import requirement_reason

    return extra_missing("modal") or requirement_reason(scope)


def daytona_probe(scope: Scope) -> str | None:
    from indus.backends.daytona import requirement_reason

    return extra_missing("daytona") or requirement_reason(scope)


def vercel_probe(scope: Scope) -> str | None:
    from indus.backends.vercel.lifecycle import requirement_reason

    return extra_missing("vercel_sandbox") or requirement_reason(scope)


async def _create_modal(request: CreateRequest) -> Environment:
    from indus.backends.modal.exec import open_modal
    from indus.backends.modal.lifecycle import KIND
    from indus.backends.sdk_ports import ProfileRecords, VendorSupport

    scope = request.scope
    return await open_modal(
        scope,
        support=VendorSupport(),
        records=ProfileRecords.for_scope(scope, KIND),
        session_id=request.session_id,
        task_key=task_key_for(request),
        overrides=request.overrides,
    )


async def _create_daytona(request: CreateRequest) -> Environment:
    from indus.backends.daytona import open_daytona
    from indus.backends.sdk_ports import VendorSupport

    return await open_daytona(
        request.scope,
        support=VendorSupport(),
        session_id=request.session_id,
        task_key=task_key_for(request),
        overrides=request.overrides,
    )


async def _create_vercel(request: CreateRequest) -> Environment:
    from indus.backends.sdk_ports import ProfileRecords, VendorSupport
    from indus.backends.vercel.exec import open_vercel
    from indus.backends.vercel.lifecycle import KIND

    scope = request.scope
    return await open_vercel(
        scope,
        support=VendorSupport(),
        records=ProfileRecords.for_scope(scope, KIND),
        session_id=request.session_id,
        task_key=task_key_for(request),
        overrides=request.overrides,
    )


def modal_entry() -> BackendEntry:
    from indus.backends.registry import BackendEntry

    return BackendEntry(
        "modal",
        _create_modal,
        MODAL_ROW_FLAGS,
        probe=modal_probe,
        profile_scoped=persistent_per_profile,
        cleanup_bound_s=SDK_CLEANUP_BOUND_S,
    )


def daytona_entry() -> BackendEntry:
    from indus.backends.registry import BackendEntry

    return BackendEntry(
        "daytona",
        _create_daytona,
        DAYTONA_ROW_FLAGS,
        probe=daytona_probe,
        profile_scoped=persistent_per_profile,
        cleanup_bound_s=SDK_CLEANUP_BOUND_S,
    )


def vercel_entry() -> BackendEntry:
    from indus.backends.registry import BackendEntry

    return BackendEntry(
        "vercel_sandbox",
        _create_vercel,
        VERCEL_ROW_FLAGS,
        probe=vercel_probe,
        profile_scoped=persistent_per_profile,
        cleanup_bound_s=SDK_CLEANUP_BOUND_S,
    )


def sdk_entries() -> list[BackendEntry]:
    """The three SDK rows, in the order the registry lists them."""
    return [modal_entry(), daytona_entry(), vercel_entry()]
