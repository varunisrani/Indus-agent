"""Print the public API surface of the pinned vendor SDKs as JSON (P21-15).

The fake SDKs in ``tests/fakes/backend/`` may only expose names the real SDKs have; their
self-test compares against the JSON this script writes. Run it by hand in a throwaway venv with
the pinned SDKs installed (it imports them, so vendor code runs), whenever a pin changes:

    <venv>/bin/python scripts/dev/sdk_surfaces.py > tests/fakes/backend/sdk_surfaces.json

Each entry is ``"<module>.<Class>"`` → the sorted public names of that class: ``dir()`` without
``_``-names, plus pydantic model fields, dataclass fields and annotated attributes. The output
also records the installed version of each distribution.
"""

from __future__ import annotations

import dataclasses
import importlib
import json
import sys
from importlib import metadata
from typing import Any

DISTRIBUTIONS = ("modal", "daytona", "vercel-sandbox")

TARGETS: tuple[str, ...] = (
    "modal.App",
    "modal.Client",
    "modal.Image",
    "modal.Sandbox",
    "modal.container_process.ContainerProcess",
    "modal.io_streams.StreamReader",
    "modal.io_streams.StreamWriter",
    "daytona.Daytona",
    "daytona.Sandbox",
    "daytona._sync.process.Process",
    "daytona._sync.filesystem.FileSystem",
    "daytona.DaytonaConfig",
    "daytona.Resources",
    "daytona.CreateSandboxFromImageParams",
    "daytona.CreateSandboxFromSnapshotParams",
    "daytona.ExecuteResponse",
    "daytona.FileUpload",
    "vercel.sandbox.sync.SyncSandboxClient",
    "vercel.sandbox.sync.SyncSandbox",
    "vercel.sandbox.sync.SyncProcess",
    "vercel.sandbox.sync.SyncSnapshot",
    "vercel.sandbox.sync.SyncSandboxFilesystem",
    "vercel.sandbox.sync.SyncTextReader",
    "vercel.sandbox.sync.SandboxServiceOptions",
    "vercel.sandbox.sync.SandboxCredentials",
    "vercel.sandbox.sync.CompletedProcess",
    "vercel.sandbox.sync.SnapshotSource",
    "vercel.sandbox.sync.SandboxResources",
)


def _resolve(dotted: str) -> Any:
    module_name, _, attr = dotted.rpartition(".")
    return getattr(importlib.import_module(module_name), attr)


def surface(cls: Any) -> list[str]:
    names = {n for n in dir(cls) if not n.startswith("_")}
    fields = getattr(cls, "model_fields", None)
    if isinstance(fields, dict):
        names.update(str(k) for k in fields)
    if dataclasses.is_dataclass(cls):
        names.update(f.name for f in dataclasses.fields(cls))
    for klass in getattr(cls, "__mro__", ()):
        annotations = vars(klass).get("__annotations__", {})
        names.update(n for n in annotations if not n.startswith("_"))
        names.update(n for n in vars(klass).get("__slots__", ()) if not n.startswith("_"))
    return sorted(names)


def main() -> int:
    out: dict[str, Any] = {
        "versions": {name: metadata.version(name) for name in DISTRIBUTIONS},
        "classes": {target: surface(_resolve(target)) for target in TARGETS},
    }
    json.dump(out, sys.stdout, indent=1, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
