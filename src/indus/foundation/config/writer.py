"""The single writer of ``config.yaml`` (R04.3, R04.4, R04.5).

`save` refuses managed installs and failed reads, re-reads the file fail-closed, keeps ``${VAR}``
templates, never writes a value equal to its default unless the user already had it, and applies
the result onto the round-trip document so comments, order and quoting survive. Nothing else under
``src/`` may serialise YAML to a config path (ratchet test in
``tests/unit/foundation/config/test_writer.py``).
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from indus.foundation import fsio, yamlio
from indus.foundation.config import backups, loader, schema
from indus.foundation.config.errors import ConfigFailedRead, ConfigManaged, ConfigUnreadable
from indus.foundation.config.loader import ConfigScope, ConfigView, FailedRead, FailedReadDict
from indus.foundation.home import is_managed

__all__ = ["save"]

_BOM = "﻿"
_DROPPED: Any = object()
_ABSENT: Any = object()
PathTuple = tuple[Any, ...]


def _read_fail_closed(path: Path) -> tuple[str | None, dict[str, Any]]:
    """The file's text and its YAML 1.1 meaning; ``(None, {})`` when it does not exist."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None, {}
    except OSError as exc:
        raise ConfigUnreadable(
            f"{path} cannot be read, so the change was not saved",
            detail=str(exc),
            hint="check the file's permissions (it should be readable by you)",
        ) from exc
    try:
        meaning = loader.read_user_tree(data)
    except yamlio.YamlError as exc:
        where = f" at line {exc.line}" if exc.line is not None else ""
        raise ConfigUnreadable(
            f"{path} has a formatting error{where}, so the change was not saved",
            detail=exc.reason,
            hint=f"fix the file, or restore a copy from {backups.backups_dir(path)}",
        ) from exc
    return data.decode("utf-8").removeprefix(_BOM), meaning


def _overlay(base: Mapping[str, Any], over: Mapping[str, Any]) -> dict[str, Any]:
    """``over`` on top of ``base`` for an additive save: mappings recurse, all else replaces."""
    out = copy.deepcopy(dict(base))
    for key, value in over.items():
        current = out.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            out[key] = _overlay(cast(dict[str, Any], current), cast(dict[str, Any], value))
        else:
            out[key] = copy.deepcopy(value)
    return out


def _keep_templates(value: Any, raw: Any, env: Mapping[str, str]) -> Any:
    """Write a raw ``${VAR}`` template back when the value equals its expansion."""
    if isinstance(value, str) and isinstance(raw, str) and "${" in raw:
        if value == raw or loader.expand_string(raw, env, {}) == value:
            return raw
        return value
    if isinstance(value, dict) and isinstance(raw, dict):
        mapping = cast(dict[Any, Any], value)
        raw_map = cast(dict[Any, Any], raw)
        return {k: _keep_templates(v, raw_map.get(k), env) for k, v in mapping.items()}
    if isinstance(value, list) and isinstance(raw, list):
        items = cast(list[Any], value)
        raw_items = cast(list[Any], raw)
        return [
            _keep_templates(v, raw_items[i] if i < len(raw_items) else None, env)
            for i, v in enumerate(items)
        ]
    return cast(Any, value)


def _leaf_paths(tree: Any, path: PathTuple = ()) -> set[PathTuple]:
    if isinstance(tree, dict) and tree:
        found: set[PathTuple] = set()
        for key, child in cast(dict[Any, Any], tree).items():
            found |= _leaf_paths(child, (*path, key))
        return found
    return {path} if path else set()


def _same(a: Any, b: Any) -> bool:
    """Equal and of the same bool-ness (``True == 1`` is a change for a YAML reader)."""
    if isinstance(a, dict) and isinstance(b, dict):
        left = cast(dict[Any, Any], a)
        right = cast(dict[Any, Any], b)
        return left.keys() == right.keys() and all(_same(left[k], right[k]) for k in left)
    if isinstance(a, list) and isinstance(b, list):
        left_items = cast(list[Any], a)
        right_items = cast(list[Any], b)
        return len(left_items) == len(right_items) and all(
            _same(x, y) for x, y in zip(left_items, right_items, strict=True)
        )
    return a == b and isinstance(a, bool) is isinstance(b, bool)


def _strip_defaults(value: Any, default: Any, path: PathTuple, explicit: set[PathTuple]) -> Any:
    if path in explicit or path == (schema.VERSION_KEY,):
        return copy.deepcopy(value)
    if isinstance(value, dict) and value:
        defaults = cast(dict[Any, Any], default) if isinstance(default, dict) else {}
        kept: dict[Any, Any] = {}
        for key, child in cast(dict[Any, Any], value).items():
            stripped = _strip_defaults(child, defaults.get(key, _ABSENT), (*path, key), explicit)
            if stripped is not _DROPPED:
                kept[key] = stripped
        return cast(Any, kept or _DROPPED)
    if isinstance(value, dict) and isinstance(default, dict):
        return _DROPPED  # an empty mapping over a default section says nothing
    if default is not _ABSENT and _same(value, default):
        return _DROPPED
    return copy.deepcopy(cast(Any, value))


# -- applying onto the round-trip document --------------------------------------------------------


def _pairing(node: Any, meaning: Any) -> dict[Any, Any]:
    """Map meaning keys (YAML 1.1) to the document's own keys; both come from the same text."""
    node_keys: list[Any] = list(node.keys())
    if isinstance(meaning, dict):
        meaning_keys = list(cast(dict[Any, Any], meaning).keys())
        if len(meaning_keys) == len(node_keys):
            return dict(zip(meaning_keys, node_keys, strict=True))
    return {k: k for k in node_keys}


def _apply_map(node: Any, meaning: Any, target: Mapping[Any, Any]) -> None:
    pairing = _pairing(node, meaning)
    old = cast(dict[Any, Any], meaning) if isinstance(meaning, dict) else {}
    for key, value in target.items():
        node_key = pairing.get(key, _ABSENT)
        if node_key is _ABSENT or node_key not in node:
            node[yamlio.roundtrip_key(key)] = yamlio.to_roundtrip(value)
            continue
        _apply_item(node, node_key, old.get(key, node[node_key]), value)
    reverse = {v: k for k, v in pairing.items()}
    existing: list[Any] = list(node.keys())
    for node_key in existing:
        if reverse.get(node_key, node_key) not in target:
            del node[node_key]


def _carry_trailing_comment(node: Any, old_last: int, new_last: int) -> None:
    """Comment lines after a list belong to its last item in the round-trip document; when items
    are appended, move them (not an end-of-line comment) to the new last item."""
    items: dict[Any, Any] | None = getattr(getattr(node, "ca", None), "items", None)
    if items is None or old_last < 0 or new_last <= old_last:
        return
    entry = items.get(old_last)
    if not entry or entry[0] is None:
        return
    token = entry[0]
    text = str(token.value)
    inline, newline, rest = ("", "", text) if text.startswith("\n") else text.partition("\n")
    if not rest:
        return
    moved = copy.copy(token)
    moved.value = rest if text.startswith("\n") else "\n" + rest
    if inline:
        token.value = inline + newline
    else:
        entry[0] = None
        if not any(entry):
            del items[old_last]
    items[new_last] = [moved, None, None, None]


def _apply_seq(node: Any, meaning: Any, target: list[Any]) -> None:
    old: list[Any] = cast(list[Any], meaning) if isinstance(meaning, list) else list(node)
    before = len(node)
    for i, value in enumerate(target):
        if i < len(node):
            _apply_item(node, i, old[i] if i < len(old) else node[i], value)
        else:
            node.append(yamlio.to_roundtrip(value))
    _carry_trailing_comment(node, before - 1, len(node) - 1)
    del node[len(target) :]


def _apply_item(container: Any, key: Any, old: Any, value: Any) -> None:
    current = container[key]
    if isinstance(value, dict) and isinstance(current, yamlio.CommentedMap):
        _apply_map(current, old, cast(dict[Any, Any], value))
    elif isinstance(value, list) and isinstance(current, yamlio.CommentedSeq):
        _apply_seq(current, old, cast(list[Any], value))
    elif not _same(old, value):
        container[key] = yamlio.to_roundtrip(value)


# -- the seam -------------------------------------------------------------------------------------


def _desired_state(
    proposed: Mapping[str, Any],
    raw: dict[str, Any],
    env: Mapping[str, str],
    *,
    replace: bool,
) -> dict[str, Any]:
    raw_canon = loader.canonicalise(raw)
    wanted = loader.canonicalise(proposed)
    target = wanted if replace else _overlay(raw_canon, wanted)
    target = cast(dict[str, Any], _keep_templates(target, raw_canon, env))
    stripped = _strip_defaults(target, schema.defaults_tree(), (), _leaf_paths(raw_canon))
    state: dict[str, Any] = {} if stripped is _DROPPED else cast(dict[str, Any], stripped)
    if schema.VERSION_KEY not in state:
        version = raw_canon.get(schema.VERSION_KEY, schema.CURRENT_VERSION)
        state = {schema.VERSION_KEY: version, **state}
    model = state.get("model")
    shorthand = raw.get("model")
    if (
        isinstance(shorthand, str)
        and shorthand
        and isinstance(model, dict)
        and list(cast(dict[str, Any], model)) == ["default"]
    ):
        state["model"] = cast(dict[str, Any], model)["default"]
    return state


def save(
    scope: ConfigScope, data: ConfigView | Mapping[str, Any], *, replace: bool = False
) -> Path:
    """Write ``data`` to the scope's ``config.yaml``; return the file's path.

    Additive by default: ``data`` is laid over the current file and nothing is deleted by
    omission. ``replace=True`` takes ``data`` as the complete desired state.
    """
    path = loader.config_path(scope)
    if is_managed(scope.home, scope.env):
        raise ConfigManaged(
            "this installation is managed, so its settings cannot be changed here",
            hint="change settings through the package manager or administrator that manages it",
        )
    if isinstance(data, FailedRead | FailedReadDict):
        raise ConfigFailedRead(
            f"{path} could not be read when these settings were loaded; saving them would "
            "replace your file with built-in defaults",
            hint="fix the file and load the settings again",
        )
    proposed = data.as_dict() if isinstance(data, ConfigView) else copy.deepcopy(dict(data))
    with loader.lock(), fsio.FileLock(path.with_name(path.name + ".lock")):
        text, meaning = _read_fail_closed(path)
        creating = text is None or not text.strip()
        if not creating:
            backups.backup_config(path, "pre-write", clock=scope.clock)
        state = _desired_state(proposed, meaning, scope.env, replace=replace)
        doc = yamlio.load_roundtrip(text or "")
        _apply_map(doc, meaning, state)
        out = yamlio.dump_roundtrip(doc)
        if creating:
            out = f"{out}\n{schema.render_example()}"
        written = fsio.atomic_write_text(path, out, follow_symlink=True)
        loader.invalidate(path)
        return written
