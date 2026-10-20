"""``/api/config`` and ``/api/env``: the dashboard's Config and Keys pages (P18-12, R04.12, R04.15).

Config: ``GET`` returns the settings as the form edits them (``config_view.form_tree``: never
env-expanded, credentials hidden); ``PUT {config}`` lays the body over the file through P01's
round-trip writer, which keeps comments, order and ``${NAME}`` references and re-reads the file
fail-closed. A file that cannot be read right now is a 500 and nothing is written; a managed install
is a 403. ``/defaults`` and ``/schema`` are public and take no profile. ``/effective`` lists every
setting with its source (default, file, env, overlay).

Keys: ``GET /api/env`` lists the known key names and every name already stored, each with set/unset
and a redacted preview only. ``PUT {key, value}`` refuses the env writer's denylist first, then
stores a credential in the profile's ``SecretStore`` (keychain by default; ``.env`` when
``security.secret_store`` is ``file``) and other settings in ``.env``; a blank value keeps what is
stored, and a value equal to the stored value's preview is refused. No answer ever carries a value.
``DELETE {key}`` removes the name from the store and from ``.env``.
"""

from __future__ import annotations

from typing import Annotated, Any, Final, NoReturn

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from indus.foundation import config as config_module
from indus.foundation import redact
from indus.foundation.config import keys as config_keys
from indus.foundation.config import schema as config_schema
from indus.foundation.config.loader import ConfigView
from indus.foundation.envfile import (
    EnvNameDenied,
    EnvNameError,
    check_name,
    check_writable_name,
    read_env_file,
    set_value,
)
from indus.foundation.envload import env_file_path
from indus.foundation.errors import ConfigError, ConfigManaged, IndusError, SecretStoreError
from indus.foundation.home import is_managed
from indus.foundation.scope import Scope
from indus.foundation.secrets import SecretStore, open_secret_store
from indus.web.routes.config_view import (
    KeyMeta,
    defaults_form,
    effective_entries,
    form_tree,
    is_credential_name,
    key_catalog,
    restore_placeholders,
    schema_form,
)
from indus.web.scope import read_scope, write_scope

__all__ = ["ROUTE_LIST", "router"]

router = APIRouter()

ROUTE_LIST: Final[tuple[tuple[str, str, bool], ...]] = (
    ("GET", "/api/config", True),
    ("PUT", "/api/config", True),
    ("GET", "/api/config/defaults", False),
    ("GET", "/api/config/schema", False),
    ("GET", "/api/config/effective", True),
    ("GET", "/api/env", True),
    ("PUT", "/api/env", True),
    ("DELETE", "/api/env", True),
)
"""``(method, path, scoped)`` of every route here (the SPA's generated route list)."""

ReadScope = Annotated[Scope, Depends(read_scope)]
WriteScope = Annotated[Scope, Depends(write_scope)]

_CUSTOM: Final = KeyMeta("custom", "", False, True)


def _fail(status: int, code: str, message: str) -> NoReturn:
    raise HTTPException(status_code=status, detail={"code": code, "message": message})


def _readable(scope: Scope) -> ConfigView:
    """The profile's config, or 500 when ``config.yaml`` cannot be read right now."""
    view = config_module.load(scope)
    if view.error is not None:
        _fail(
            500,
            "config_unreadable",
            "config.yaml cannot be read right now (a formatting or permission problem); fix the "
            "file first. Nothing was changed.",
        )
    return view


def _refuse_managed(scope: Scope) -> None:
    if is_managed(scope.home, scope.env):
        _fail(
            403,
            "config_managed",
            "This installation is managed; change its settings where it is managed.",
        )


# -- config ---------------------------------------------------------------------------------------


class ConfigUpdate(BaseModel):
    config: dict[str, Any]


@router.get("/api/config")
def get_config(scope: ReadScope) -> dict[str, Any]:
    return form_tree(_readable(scope))


@router.put("/api/config")
def put_config(body: ConfigUpdate, scope: WriteScope) -> dict[str, Any]:
    _refuse_managed(scope)
    view = _readable(scope)
    wanted = restore_placeholders(body.config, view.user_raw())
    wanted = {k: v for k, v in wanted.items() if not k.startswith("_")}
    try:
        config_module.save(scope, wanted)
    except ConfigManaged:
        _fail(403, "config_managed", "This installation is managed; nothing was changed.")
    except ConfigError as exc:
        _fail(500, "config_unreadable", f"{exc.message}. Nothing was changed.")
    saved = config_module.load(scope)
    return {"ok": True, "warnings": config_schema.validate(saved.as_dict())}


@router.get("/api/config/defaults")
def get_defaults() -> dict[str, Any]:
    return defaults_form()


@router.get("/api/config/schema")
def get_schema() -> dict[str, Any]:
    return schema_form()


@router.get("/api/config/effective")
def get_effective(scope: ReadScope) -> dict[str, Any]:
    return {"entries": effective_entries(_readable(scope))}


# -- keys -----------------------------------------------------------------------------------------


class EnvUpdate(BaseModel):
    key: str = Field(min_length=1, max_length=128)
    value: str = Field(max_length=16384)


class EnvDelete(BaseModel):
    key: str = Field(min_length=1, max_length=128)


def _secret_mode(scope: Scope) -> str:
    view = config_module.load(scope)
    return str(view.get("security.secret_store", "auto") or "auto")


def _store(scope: Scope) -> SecretStore:
    if isinstance(scope.secrets, SecretStore):
        return scope.secrets
    try:
        return open_secret_store(
            scope.home, scope.profile_id, mode=_secret_mode(scope), clock=scope.clock
        )
    except SecretStoreError:
        _fail(503, "secret_store_unavailable", "The secret store cannot be opened right now.")


def _preview(value: str | None) -> str | None:
    """Masked, for settings too: an endpoint URL may embed a token."""
    return redact.mask(value) if value else None


def _row(meta: KeyMeta, value: str | None) -> dict[str, Any]:
    return {
        "category": meta.category,
        "description": meta.description,
        "advanced": meta.advanced,
        "is_password": meta.is_password,
        "is_set": bool(value),
        "redacted_value": _preview(value),
        "url": meta.url,
    }


def _stored_value(store: SecretStore, file_values: dict[str, str], name: str) -> str | None:
    try:
        found = store.get(name)
    except (SecretStoreError, ValueError):
        found = None
    return found if found else file_values.get(name)


@router.get("/api/env")
def list_keys(scope: ReadScope) -> dict[str, Any]:
    store = _store(scope)
    file_values = read_env_file(env_file_path(scope.home))
    catalog = key_catalog()
    try:
        stored_names = store.names()
    except SecretStoreError:
        stored_names = []
    names = [*catalog, *sorted({*stored_names, *file_values} - set(catalog))]
    return {
        name: _row(catalog.get(name, _CUSTOM), _stored_value(store, file_values, name))
        for name in names
    }


def _check_key(key: str) -> str:
    if not config_keys.is_env_shaped(key):
        _fail(400, "invalid_key", "Key names are UPPER_SNAKE_CASE, like OPENAI_API_KEY.")
    try:
        check_writable_name(key)
    except EnvNameDenied as exc:
        _fail(400, "env_name_denied", str(exc))
    except EnvNameError:
        _fail(400, "invalid_key", "That is not a valid key name.")
    return key


def _answer(key: str, value: str | None, where: str | None) -> dict[str, Any]:
    return {
        "key": key,
        "is_set": bool(value),
        "redacted_value": _preview(value),
        "stored_in": where,
    }


@router.put("/api/env")
def put_key(body: EnvUpdate, scope: WriteScope) -> dict[str, Any]:
    key = _check_key(body.key)
    _refuse_managed(scope)
    env_path = env_file_path(scope.home)
    store = _store(scope)
    file_values = read_env_file(env_path)
    current = _stored_value(store, file_values, key)
    value = body.value.strip()
    if not value:  # blank keeps what is stored
        return _answer(key, current, None)
    if current and value in (redact.mask(current), redact.MASK_SHORT):
        _fail(
            400,
            "redacted_value",
            "That is the hidden preview of the stored value, not a key; paste the real one.",
        )
    to_store = is_credential_name(key) and _secret_mode(scope) != "file"
    try:
        if to_store:
            store.set(key, value)
            if key in file_values:
                set_value(env_path, key, None)  # the store holds it now; drop the plain copy
            where = store.backend_name
        else:
            set_value(env_path, key, value)
            where = "env_file"
    except IndusError:
        _fail(503, "secret_store_unavailable", f"{key} could not be saved; nothing was changed.")
    except OSError:
        _fail(500, "env_write_failed", f"{key} could not be written to the profile's .env file.")
    return _answer(key, value, where)


@router.delete("/api/env")
def delete_key(body: EnvDelete, scope: WriteScope) -> dict[str, Any]:
    key = body.key
    try:
        check_name(key)
    except EnvNameError:
        _fail(400, "invalid_key", "That is not a valid key name.")
    _refuse_managed(scope)
    env_path = env_file_path(scope.home)
    store = _store(scope)
    removed = False
    try:
        removed = store.delete(key) or removed
    except (SecretStoreError, ValueError):
        _fail(503, "secret_store_unavailable", f"{key} could not be removed; try again.")
    if key in read_env_file(env_path):
        try:
            set_value(env_path, key, None)
        except OSError:
            _fail(500, "env_write_failed", f"{key} could not be removed from the .env file.")
        removed = True
    return {"ok": True, "key": key, "removed": removed}
