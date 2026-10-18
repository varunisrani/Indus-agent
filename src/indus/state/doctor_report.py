"""The milestone-0 doctor report: home, config, secrets, store and logs, read-only.

``python -m indus.state.doctor_report [-p NAME] [--json]`` boots the process the normal way
(``foundation.bootstrap.run``: profile selection, ``.env``, config, secrets) and prints five
sections. ``minimal_report(scope)`` builds the same data for ``indus doctor --minimal`` (P07-19).

- home: the profile home, its id and how it was selected (flag, env, sticky or default);
- config: the file, ``_config_version``, each key the user set with its source (``user`` or
  ``env-expanded``) and how many settings are left at their defaults — names only, never values;
- secrets: the ``security.secret_store`` setting, the backend in use and the keychain probe;
- store: path, journal mode, SQLite version, FTS5 / trigram availability, schema version and the
  health from ``browse`` with its plain-language line — opened read-only, never created;
- logs: the logs folder.

Exit code 0 when every section is fine, 1 when any is ``corrupt`` or ``unavailable`` (or the
process could not start), never anything else. Labels are mirrored in ``locales/en.yaml`` under
``cli.doctor``.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from indus.foundation import bootstrap, preparse
from indus.foundation import config as config_mod
from indus.foundation.config import keys as config_keys
from indus.foundation.config.schema import VERSION_KEY
from indus.foundation.errors import IndusError
from indus.foundation.log import logs_dir
from indus.foundation.scope import Scope
from indus.foundation.secrets import probe_keychain
from indus.state import browse, fts

__all__ = ["LABELS", "Report", "Section", "Status", "main", "minimal_report"]

Status = Literal["ok", "warn", "missing", "busy", "corrupt", "unavailable"]
FAILING: frozenset[str] = frozenset({"corrupt", "unavailable"})

LABELS: dict[str, str] = {
    "title": "Indus Agent doctor (minimal report)",
    "home": "home: {path} (profile {profile}, selected by {how})",
    "config": "config: {path} (version {version}; {user} set by you, {defaults} at defaults)",
    "config_absent": "config: {path} (no file yet; all {defaults} settings at defaults)",
    "config_key": "  {key}: {source}",
    "config_broken": "  the file could not be read, the last good settings are used: {reason}",
    "secrets": "secrets: {backend} backend (setting {mode}; keychain {keychain})",
    "keychain_yes": "available",
    "keychain_no": "not available",
    "store": "store: {state}",
    "store_details": "store: {state} ({mode}, schema {schema}, fts5 {fts5}, trigram {trigram})",
    "store_file": "  file: {path}",
    "store_sqlite": "  SQLite {version} (fts5 {fts5}, trigram {trigram})",
    "store_line": "  {line}",
    "logs": "logs: {path}",
    "word_yes": "yes",
    "word_no": "no",
    "failed": "doctor could not start: {reason}",
    "usage": "usage: python -m indus.state.doctor_report [-p NAME] [--json]",
}


@dataclass
class Section:
    name: str
    status: Status
    facts: dict[str, Any] = field(default_factory=lambda: {})
    lines: list[str] = field(default_factory=lambda: [])


@dataclass
class Report:
    sections: list[Section]

    @property
    def exit_code(self) -> int:
        return 1 if any(s.status in FAILING for s in self.sections) else 0

    @property
    def status(self) -> str:
        return "failing" if self.exit_code else "ok"

    def section(self, name: str) -> Section:
        return next(s for s in self.sections if s.name == name)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {s.name: {"status": s.status, **s.facts} for s in self.sections}
        out["status"] = self.status
        out["exit_code"] = self.exit_code
        return out

    def text(self) -> str:
        return "\n".join([LABELS["title"], *(line for s in self.sections for line in s.lines)])


def _yes(flag: object) -> str:
    return LABELS["word_yes"] if flag else LABELS["word_no"]


# -- sections -----------------------------------------------------------------------------------


def _home(scope: Scope, how: str) -> Section:
    facts = {"path": str(scope.home), "profile": scope.profile_id, "selected_by": how}
    line = LABELS["home"].format(path=scope.home, profile=scope.profile_id, how=how)
    return Section("home", "ok", facts, [line])


def _leaves(tree: Mapping[str, Any], prefix: tuple[str, ...] = ()) -> Iterator[tuple[str, ...]]:
    for name, value in tree.items():
        path = (*prefix, str(name))
        if isinstance(value, Mapping) and value:
            yield from _leaves(value, path)  # pyright: ignore[reportUnknownArgumentType]
        else:
            yield path


def _config(scope: Scope) -> Section:
    view = scope.config if isinstance(scope.config, config_mod.ConfigView) else None
    if view is None:
        view = config_mod.load(scope)
    user_keys: list[dict[str, str]] = []
    for parts in _leaves(view.user_raw()):
        key = config_keys.join_key(list(parts))
        if key == VERSION_KEY:
            continue
        user_keys.append({"key": key, "source": str(view.source(key) or "user")})
    set_paths = {entry["key"] for entry in user_keys}
    defaults = sum(
        1
        for parts in _leaves(config_mod.defaults_tree())
        if config_keys.join_key(list(parts)) not in set_paths
        and config_keys.join_key(list(parts)) != VERSION_KEY
    )
    exists = view.path.is_file()
    version = view.get(VERSION_KEY)
    facts: dict[str, Any] = {
        "path": str(view.path),
        "exists": exists,
        "version": version,
        "user_keys": user_keys,
        "defaults": defaults,
        "error": None if view.error is None else str(view.error),
    }
    status: Status = "warn" if view.failed or view.error is not None else "ok"
    if exists:
        lines = [
            LABELS["config"].format(
                path=view.path, version=version, user=len(user_keys), defaults=defaults
            )
        ]
    else:
        lines = [LABELS["config_absent"].format(path=view.path, defaults=defaults)]
    lines += [LABELS["config_key"].format(**entry) for entry in user_keys]
    if view.error is not None:
        lines.append(LABELS["config_broken"].format(reason=view.error))
    return Section("config", status, facts, lines)


def _secrets(scope: Scope) -> Section:
    mode = "auto"
    if scope.config is not None:
        mode = str(scope.config.get("security.secret_store") or "auto")
    backend = str(getattr(scope.secrets, "backend_name", None) or "none")
    keychain = probe_keychain() is not None
    facts = {"setting": mode, "backend": backend, "keychain": keychain}
    word = LABELS["keychain_yes"] if keychain else LABELS["keychain_no"]
    line = LABELS["secrets"].format(backend=backend, mode=mode, keychain=word)
    return Section("secrets", "ok", facts, [line])


def _store(scope: Scope) -> Section:
    report = browse.health(scope)
    library = {"fts5": fts.fts5_available(), "trigram": fts.trigram_supported()}
    facts: dict[str, Any] = {
        "path": str(report.path),
        "state": report.state,
        "journal_mode": report.journal_mode,
        "schema_version": report.schema_version,
        "fts5": report.fts,
        "trigram": report.trigram,
        "fts_stale": report.fts_stale,
        "bucket": None if report.bucket is None else report.bucket.value,
        "kind": report.kind,
        "line": report.line,
        "sqlite": {"version": report.sqlite_version, **library},
    }
    if report.state == "ok":
        head = LABELS["store_details"].format(
            state=report.state,
            mode=report.journal_mode,
            schema=report.schema_version,
            fts5=_yes(report.fts),
            trigram=_yes(report.trigram),
        )
    else:
        head = LABELS["store"].format(state=report.state)
    lines = [
        head,
        LABELS["store_file"].format(path=report.path),
        LABELS["store_sqlite"].format(
            version=report.sqlite_version,
            fts5=_yes(library["fts5"]),
            trigram=_yes(library["trigram"]),
        ),
    ]
    if report.line:
        lines.append(LABELS["store_line"].format(line=report.line))
    status: Status
    if report.state == "ok":
        status = "warn" if report.bucket is not None else "ok"
    elif report.state == "busy":
        status = "busy"
    else:
        status = report.state
    return Section("store", status, facts, lines)


def _logs(scope: Scope) -> Section:
    path = logs_dir(scope.home)
    facts = {"path": str(path), "exists": path.is_dir()}
    return Section(
        "logs", "ok" if path.is_dir() else "warn", facts, [LABELS["logs"].format(path=path)]
    )


def minimal_report(scope: Scope, *, selected_by: str = "unknown") -> Report:
    """The five sections for ``scope``; reads only (the store through ``browse``)."""
    return Report(
        [_home(scope, selected_by), _config(scope), _secrets(scope), _store(scope), _logs(scope)]
    )


# -- module entry -------------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        how = preparse.select_profile(args, dict(os.environ)).source
        scope, rest = bootstrap.run(args)
    except IndusError as exc:
        print(LABELS["failed"].format(reason=exc), file=sys.stderr)
        return 1
    unknown = [arg for arg in rest if arg != "--json"]
    if unknown:
        print(LABELS["usage"], file=sys.stderr)
        return 1
    report = minimal_report(scope, selected_by=how)
    if "--json" in rest:
        print(json.dumps(report.as_dict(), indent=2, default=str))
    else:
        print(report.text())
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
