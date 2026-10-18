"""An in-memory ``SecretStore`` stand-in: same names rule, never the OS keychain or a file."""

from __future__ import annotations

from indus.foundation.envfile import NAME_RE


class MemorySecretStore:
    """``get``/``set``/``delete``/``names`` like ``foundation.secrets.SecretStore``."""

    backend_name = "memory"

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values: dict[str, str] = dict(values or {})
        self._generation = 0
        self.fail_next_set = False

    def __repr__(self) -> str:
        return f"MemorySecretStore(names={sorted(self.values)})"

    @staticmethod
    def _check(name: str) -> None:
        if not NAME_RE.match(name):
            raise ValueError(f"{name!r} is not a valid secret name")

    @property
    def generation(self) -> int:
        return self._generation

    def get(self, name: str) -> str | None:
        self._check(name)
        return self.values.get(name)

    def set(self, name: str, value: str) -> None:
        self._check(name)
        if self.fail_next_set:
            self.fail_next_set = False
            raise OSError("fake secret store write failure")
        self.values[name] = value
        self._generation += 1

    def delete(self, name: str) -> bool:
        self._check(name)
        self._generation += 1
        return self.values.pop(name, None) is not None

    def names(self) -> list[str]:
        return sorted(self.values)

    def known_values(self) -> list[str]:
        return [v for v in self.values.values() if v]
