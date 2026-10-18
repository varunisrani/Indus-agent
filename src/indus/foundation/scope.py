"""``Scope``: which profile a piece of work belongs to, carried in a context variable.

One process may serve several profiles. Everything that needs the home, env, config, secrets or
clock reads them from the current ``Scope`` instead of process-global state. ``current_scope()``
raises ``ScopeUnresolved`` when no scope is set: silently using the default profile is how data
ends up in the wrong profile (R03.23). Background work keeps its profile by copying the context
(``spawn_with_scope``, ``thread_with_scope``, ``bind_scope``; R01.16).
"""

from __future__ import annotations

import asyncio
import contextvars
import os
import threading
from collections.abc import Callable, Coroutine, Generator, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol

from indus.foundation.clock import Clock, SystemClock
from indus.foundation.errors import ScopeUnresolved
from indus.foundation.home import HOME_ENV, root_of
from indus.foundation.profiles import profile_id_of

_EMPTY: Mapping[str, Any] = MappingProxyType({})


class EnvView(Mapping[str, str]):
    """Immutable env: a snapshot plus an overlay (``.env`` publishing) where None removes a key."""

    __slots__ = ("_base", "_overlay", "_merged")

    def __init__(
        self,
        base: Mapping[str, str] | None = None,
        overlay: Mapping[str, str | None] | None = None,
    ) -> None:
        self._base: Mapping[str, str] = MappingProxyType(dict(base or {}))
        self._overlay: Mapping[str, str | None] = MappingProxyType(dict(overlay or {}))
        merged = dict(self._base)
        for key, value in self._overlay.items():
            if value is None:
                merged.pop(key, None)
            else:
                merged[key] = value
        self._merged: Mapping[str, str] = MappingProxyType(merged)

    @classmethod
    def from_process(cls) -> EnvView:
        """A snapshot of ``os.environ`` now; later process changes do not show through."""
        return cls(dict(os.environ))

    @property
    def base(self) -> Mapping[str, str]:
        return self._base

    @property
    def overlay(self) -> Mapping[str, str | None]:
        return self._overlay

    def with_overlay(self, changes: Mapping[str, str | None]) -> EnvView:
        """A new view with ``changes`` applied on top of the current overlay."""
        return EnvView(self._base, {**self._overlay, **changes})

    def to_dict(self) -> dict[str, str]:
        return dict(self._merged)

    def __getitem__(self, key: str) -> str:
        return self._merged[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._merged)

    def __len__(self) -> int:
        return len(self._merged)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, EnvView):
            return self._merged == other._merged
        return NotImplemented

    def __hash__(self) -> int:
        return hash(frozenset(self._merged.items()))

    def __repr__(self) -> str:
        # Names only: values may be secrets.
        return f"EnvView({len(self._merged)} names)"


class ConfigSource(Protocol):
    """What the scope needs from a config view (P01-10 ``ConfigView`` fits)."""

    def get(self, path: str, /) -> object: ...


class SecretSource(Protocol):
    """What the scope needs from a secret store (P01-16 ``SecretStore`` fits).

    ``known_values`` and ``generation`` feed exact-value redaction (P01-17).
    """

    def get(self, name: str, /) -> str | None: ...

    def known_values(self) -> list[str]: ...

    @property
    def generation(self) -> int: ...


@dataclass(frozen=True)
class Scope:
    profile_id: str
    home: Path
    root: Path
    env: EnvView
    clock: Clock
    config: ConfigSource | None = None
    secrets: SecretSource | None = None
    terminal_policy: Mapping[str, Any] = field(default_factory=lambda: _EMPTY)
    # True when this scope serves a profile other than the process's own (multi-profile hosts);
    # process-level env such as INDUS_TIMEZONE then does not speak for it.
    routed: bool = False

    def with_config(self, config: ConfigSource | None) -> Scope:
        return replace(self, config=config)

    def with_secrets(self, secrets: SecretSource | None) -> Scope:
        return replace(self, secrets=secrets)

    def with_env(self, env: EnvView) -> Scope:
        return replace(self, env=env)


def make_scope(
    home: Path,
    *,
    env: EnvView | Mapping[str, str] | None = None,
    clock: Clock | None = None,
    routed: bool = False,
) -> Scope:
    """A scope for ``home``; profile id and root follow from the path shape."""
    view = env if isinstance(env, EnvView) else EnvView(dict(env) if env is not None else {})
    return Scope(
        profile_id=profile_id_of(home),
        home=home,
        root=root_of(home),
        env=view,
        clock=clock if clock is not None else SystemClock(),
        routed=routed,
    )


# -- context ------------------------------------------------------------------------------------

_SCOPE: contextvars.ContextVar[Scope | None] = contextvars.ContextVar("indus_scope", default=None)


def current_scope() -> Scope:
    scope = _SCOPE.get()
    if scope is None:
        raise ScopeUnresolved(
            "no profile scope is set here",
            hint="run this work inside use_scope(...) or start it with spawn_with_scope / "
            "thread_with_scope so it keeps its profile",
        )
    return scope


def scope_or_none() -> Scope | None:
    return _SCOPE.get()


@contextmanager
def use_scope(scope: Scope) -> Generator[Scope]:
    token = _SCOPE.set(scope)
    try:
        yield scope
    finally:
        _SCOPE.reset(token)


def _context_for(scope: Scope | None) -> contextvars.Context:
    ctx = contextvars.copy_context()
    if scope is not None:
        ctx.run(_SCOPE.set, scope)
    return ctx


def spawn_with_scope[T](
    coro: Coroutine[Any, Any, T], *, scope: Scope | None = None, name: str | None = None
) -> asyncio.Task[T]:
    """Start ``coro`` as a task running in a copy of this context (or with ``scope``)."""
    loop = asyncio.get_running_loop()
    return loop.create_task(coro, name=name, context=_context_for(scope))


def bind_scope[**P, R](fn: Callable[P, R], *, scope: Scope | None = None) -> Callable[P, R]:
    """Wrap ``fn`` so every call runs in a fresh copy of the context captured now."""
    captured = _context_for(scope)

    def bound(*args: P.args, **kwargs: P.kwargs) -> R:
        return captured.copy().run(fn, *args, **kwargs)

    return bound


def thread_with_scope(
    fn: Callable[..., object],
    *args: object,
    scope: Scope | None = None,
    name: str | None = None,
    daemon: bool | None = None,
) -> threading.Thread:
    """An unstarted thread that runs ``fn(*args)`` with this context's scope (or ``scope``)."""
    return threading.Thread(target=bind_scope(fn, scope=scope), args=args, name=name, daemon=daemon)


def child_env(scope: Scope) -> dict[str, str]:
    """The minimal profile pinning a subprocess needs: ``INDUS_HOME`` always set explicitly.

    Full child environments (allow-lists, secret stripping) are built by ``safety`` in P05.
    """
    return {HOME_ENV: str(scope.home)}
