"""Write or check ``defaults.json``, the generated twin of the config ``DEFAULTS`` (W50-f-1).

The models in ``schema.py`` stay the one source of every default. Building ``DEFAULTS`` from them
at import forced pydantic to build every section's validator, about 100 ms of every start; the
package also ships the built table as JSON with the sha256 of ``schema.py`` and ``versions.py``,
and ``schema`` reads the twin only while that digest matches.

Usage: ``uv run python -m indus.foundation.config.defaults_twin`` rewrites the twin; ``--check``
exits 1 when it is stale (a unit test and the ``generated_twins`` guard run the same check).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import Any

from indus.foundation.config import schema

NOTE = (
    "generated from foundation/config/schema.py by "
    "`python -m indus.foundation.config.defaults_twin`; do not edit"
)


class TwinError(ValueError):
    """The defaults cannot be carried by JSON without changing a value or a type."""


def render(defaults: dict[str, Any], digest: str) -> str:
    """The twin's text for ``defaults`` built from sources with ``digest``."""
    twin = {"note": NOTE, "source_sha256": digest, "defaults": defaults}
    text = json.dumps(twin, indent=1, ensure_ascii=False) + "\n"
    if not schema.same_value(json.loads(text)["defaults"], defaults):
        raise TwinError("a default is not plain JSON (tuple, set, date...); keep it a list/str")
    return text


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m indus.foundation.config.defaults_twin",
        description=(__doc__ or "").splitlines()[0],
    )
    parser.add_argument("--check", action="store_true", help="exit 1 if the twin is stale")
    args = parser.parse_args(argv)
    wanted = render(schema.build_defaults(), schema.source_digest())
    target = schema.DEFAULTS_TWIN
    current = target.read_text(encoding="utf-8") if target.is_file() else None
    if args.check:
        if current == wanted:
            return 0
        sys.stdout.write(
            f"{target.name} is stale; run "
            "`uv run python -m indus.foundation.config.defaults_twin`\n"
        )
        return 1
    if current != wanted:
        target.write_text(wanted, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
