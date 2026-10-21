"""An in-memory GHCR for the publication controller tests (P23-23).

``manifests[repository][tag] = digest``. ``tag()`` points a new tag at an existing digest (like
``docker buildx imagetools create``); ``fail_once("tag")`` breaks the next call. ``events`` may be
shared with the fake GitHub's ``calls`` so a test can check the order across both services.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class RegistryFailure(RuntimeError):
    pass


@dataclass
class FakeRegistry:
    manifests: dict[str, dict[str, str]] = field(default_factory=dict[str, dict[str, str]])
    events: list[str] = field(default_factory=list[str])
    _failures: dict[str, int] = field(default_factory=dict[str, int])

    def fail_once(self, op: str) -> None:
        self._failures[op] = self._failures.get(op, 0) + 1

    def push(self, repository: str, tag: str, digest: str) -> None:
        self.manifests.setdefault(repository, {})[tag] = digest

    def digest(self, repository: str, tag: str) -> str | None:
        return self.manifests.get(repository, {}).get(tag)

    def tag(self, repository: str, digest: str, tag: str) -> None:
        if self._failures.get("tag"):
            self._failures["tag"] -= 1
            raise RegistryFailure(f"injected failure tagging {repository}:{tag}")
        if digest not in self.manifests.get(repository, {}).values():
            raise RegistryFailure(f"{repository}@{digest} was never pushed")
        self.events.append(f"alias:{repository}:{tag}")
        self.manifests[repository][tag] = digest
