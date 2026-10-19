# pyright: strict
"""Modal direct mode: settings, credentials, sandbox create / restore / snapshot / terminate
(P21-05).

Mode: ``terminal.modal_mode`` is ``auto`` or ``direct`` (same meaning). ``managed`` (the
vendor-hosted gateway) is not offered (D-16.16): refused by config validation and again here.

Credentials: the user's token pair, ``MODAL_TOKEN_ID`` + ``MODAL_TOKEN_SECRET``, from the profile's
secret store or else the scope env (D-16.22). The SDK client is built from the pair in memory, so
neither half ever reaches a process env, argv or log. Missing → ``requirement_reason`` says so.

Create: the app ``indus-agent`` (looked up, created when missing), a sandbox running
``sleep infinity`` for up to ``SANDBOX_LIFETIME_S`` with the container CPU and memory settings,
within ``CREATE_TIMEOUT_S``. A persistent profile first restores the snapshot recorded for its task
key; a failed restore drops that record (only while it still names the same snapshot), creates
from the base image and sets ``recreated`` (R02.19). The base image is ``terminal.modal_image``,
else ``terminal.docker_image`` (our sandbox image, D-16.64); ``im-…`` names an image id.

Close: persistent → filesystem snapshot (kept until we replace it, ``SNAPSHOT_OP_TIMEOUT_S``) and
its id recorded; then terminate (``STOP_TIMEOUT_S``). Failures are logged; terminate always runs.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from indus.backends.contract import BackendUnavailable
from indus.backends.sdk_shell import (
    CREATE_TIMEOUT_S,
    OP_CREATE,
    OP_GET,
    OP_SNAPSHOT,
    OP_STOP,
    SNAPSHOT_OP_TIMEOUT_S,
    STOP_TIMEOUT_S,
    SandboxRecords,
    SdkSupport,
    config_value,
    credential,
)
from indus.foundation.config.schema import MODAL_MANAGED_REFUSED
from indus.foundation.scope import Scope

__all__ = [
    "APP_NAME",
    "KIND",
    "SANDBOX_LIFETIME_S",
    "TOKEN_ID",
    "TOKEN_SECRET",
    "ModalSandbox",
    "ModalSettings",
    "credentials",
    "requirement_reason",
    "settings_from",
]

log = logging.getLogger(__name__)

TOKEN_ID = "MODAL_TOKEN_ID"
TOKEN_SECRET = "MODAL_TOKEN_SECRET"
APP_NAME = "indus-agent"
SANDBOX_LIFETIME_S = 3600
LOOKUP_TIMEOUT_S = 60.0
SNAPSHOT_SDK_TIMEOUT_S = 55  # the SDK's own bound, inside our 60 s deadline
KIND = "modal"


@dataclass(frozen=True, slots=True)
class ModalSettings:
    image: str
    cpu: float
    memory_mb: int
    persistent: bool
    timeout: float
    cwd: str
    task_key: str = "default"


def _mode(scope: Scope) -> str:
    raw = str(config_value(scope, "terminal.modal_mode") or "auto").strip().lower()
    if raw == "managed":
        raise BackendUnavailable(MODAL_MANAGED_REFUSED)
    if raw not in ("auto", "direct"):
        raise BackendUnavailable(f"terminal.modal_mode must be auto or direct, not {raw!r}")
    return raw


def settings_from(
    scope: Scope, *, task_key: str = "default", overrides: Mapping[str, Any] | None = None
) -> ModalSettings:
    """The Modal settings of ``scope`` (``BackendUnavailable`` for ``managed``)."""
    _mode(scope)
    over = overrides or {}
    image = str(
        over.get("image")
        or config_value(scope, "terminal.modal_image")
        or config_value(scope, "terminal.docker_image")
    ).strip()
    raw_cwd = str(over.get("cwd") or config_value(scope, "terminal.cwd") or "").strip()
    return ModalSettings(
        image=image,
        cpu=float(config_value(scope, "terminal.container_cpu")),
        memory_mb=int(config_value(scope, "terminal.container_memory_mb")),
        persistent=bool(config_value(scope, "terminal.container_persistent")),
        timeout=float(config_value(scope, "terminal.timeout")),
        cwd="~" if raw_cwd in ("", ".") else raw_cwd,
        task_key=task_key,
    )


def credentials(scope: Scope) -> tuple[str, str] | None:
    token_id = credential(scope, TOKEN_ID)
    token_secret = credential(scope, TOKEN_SECRET)
    if token_id and token_secret:
        return token_id, token_secret
    return None


def requirement_reason(scope: Scope) -> str | None:
    """Why Modal cannot run for ``scope`` (mode, token pair), or None. No network, no import."""
    try:
        _mode(scope)
    except BackendUnavailable as exc:
        return str(exc)
    if credentials(scope) is None:
        return (
            f"the modal backend needs your Modal token pair: store {TOKEN_ID} and {TOKEN_SECRET} "
            "in this profile's secret store (or set them in the environment)"
        )
    return None


class ModalSandbox:
    """One Modal sandbox's lifecycle (see the module notes). ``sdk`` is the ``modal`` module (or
    a fake with the same surface)."""

    def __init__(
        self,
        sdk: Any,
        support: SdkSupport,
        records: SandboxRecords,
        settings: ModalSettings,
    ) -> None:
        self.sdk = sdk
        self.support = support
        self.records = records
        self.settings = settings
        self.client: Any = None
        self.app: Any = None
        self.sandbox: Any = None
        self.recreated = False
        self.restored_from: str | None = None

    async def open(self, token_id: str, token_secret: str) -> None:
        sdk = self.sdk
        self.client = await self.support.call(
            OP_GET,
            lambda: sdk.Client.from_credentials(token_id, token_secret),
            timeout=LOOKUP_TIMEOUT_S,
            idempotent=True,
        )
        client = self.client
        self.app = await self.support.call(
            OP_GET,
            lambda: sdk.App.lookup(APP_NAME, create_if_missing=True, client=client),
            timeout=LOOKUP_TIMEOUT_S,
            idempotent=True,
        )
        snapshot_id = await self._restore_candidate()
        if snapshot_id is not None:
            try:
                self.sandbox = await self._create(
                    lambda: sdk.Image.from_id(snapshot_id, client=client)
                )
                self.restored_from = snapshot_id
                log.info("modal: restored the sandbox from snapshot %s", snapshot_id[:24])
            except Exception as exc:
                log.warning(
                    "modal: restoring snapshot %s failed (%s); starting from the base image",
                    snapshot_id[:24],
                    type(exc).__name__,
                )
                await self._forget(snapshot_id)
                self.recreated = True
        if self.sandbox is None:
            self.sandbox = await self._create(self._base_image)
        log.info("modal: sandbox ready (task %s)", self.settings.task_key)

    async def _forget(self, snapshot_id: str) -> None:
        """Drop the record of a snapshot that could not be restored (only while it still names
        that snapshot)."""
        try:
            await self.records.drop(self.settings.task_key, expect={"snapshot_id": snapshot_id})
        except Exception as exc:
            log.warning("%s: could not drop the stale snapshot record (%s)", KIND, exc)

    async def _restore_candidate(self) -> str | None:
        if not self.settings.persistent:
            return None
        record = await self.records.load(self.settings.task_key) or {}
        value = record.get("snapshot_id")
        return value if isinstance(value, str) and value else None

    def _base_image(self) -> Any:
        image = self.settings.image
        if image.startswith("im-"):
            return self.sdk.Image.from_id(image, client=self.client)
        return self.sdk.Image.from_registry(image)

    async def _create(self, image_for: Any) -> Any:
        sdk = self.sdk
        settings = self.settings

        def make() -> Any:
            return sdk.Sandbox.create(
                "sleep",
                "infinity",
                app=self.app,
                image=image_for(),
                timeout=SANDBOX_LIFETIME_S,
                cpu=settings.cpu,
                memory=settings.memory_mb,
                client=self.client,
            )

        return await self.support.call(OP_CREATE, make, timeout=CREATE_TIMEOUT_S)

    async def close(self) -> None:
        sandbox = self.sandbox
        if sandbox is None:
            return
        self.sandbox = None
        if self.settings.persistent:
            try:
                image = await self.support.call(
                    OP_SNAPSHOT,
                    lambda: sandbox.snapshot_filesystem(timeout=SNAPSHOT_SDK_TIMEOUT_S, ttl=None),
                    timeout=SNAPSHOT_OP_TIMEOUT_S,
                )
                snapshot_id = str(getattr(image, "object_id", "") or "")
                if snapshot_id:
                    await self.records.save(self.settings.task_key, {"snapshot_id": snapshot_id})
                    log.info("modal: saved snapshot %s", snapshot_id[:24])
                else:
                    log.warning("modal: the snapshot came back without an id; nothing recorded")
            except Exception as exc:
                log.warning("modal: filesystem snapshot failed (%s)", type(exc).__name__)
        try:
            await self.support.call(OP_STOP, sandbox.terminate, timeout=STOP_TIMEOUT_S)
        except Exception as exc:
            log.warning("modal: terminating the sandbox failed (%s)", type(exc).__name__)
