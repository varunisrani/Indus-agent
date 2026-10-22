"""Write or check ``pricing_table.json``, the generated twin of ``pricing_table.yaml`` (W48-g-1).

The YAML stays the one source a person edits. Parsing it with the pure-Python YAML loader cost
about 50 ms of every run's first turn, so the package also ships the parsed data as JSON with the
YAML's sha256; ``pricing.default_table`` reads the twin only while that digest matches.

Usage: ``uv run python -m indus.providers.pricing_twin`` rewrites the twin; ``--check`` exits 1
when it is stale (a unit test runs the same check).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from indus.foundation import yamlio
from indus.providers.pricing import TABLE_FILE, TWIN_FILE, source_digest, table_from_data

HERE = Path(__file__).resolve().parent
NOTE = "generated from pricing_table.yaml by `python -m indus.providers.pricing_twin`; do not edit"


def render(yaml_text: str) -> str:
    """The twin's text for ``yaml_text`` (refuses a table ``pricing`` would refuse)."""
    data = yamlio.load_safe(yaml_text)
    table_from_data(data)
    twin = {"note": NOTE, "source_sha256": source_digest(yaml_text), "table": data}
    return json.dumps(twin, indent=1, ensure_ascii=False) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m indus.providers.pricing_twin", description=(__doc__ or "").splitlines()[0]
    )
    parser.add_argument("--check", action="store_true", help="exit 1 if the twin is stale")
    args = parser.parse_args(argv)
    source = (HERE / TABLE_FILE).read_text(encoding="utf-8")
    target = HERE / TWIN_FILE
    wanted = render(source)
    current = target.read_text(encoding="utf-8") if target.is_file() else None
    if args.check:
        if current == wanted:
            return 0
        sys.stdout.write(
            f"{TWIN_FILE} is stale; run `uv run python -m indus.providers.pricing_twin`\n"
        )
        return 1
    if current != wanted:
        target.write_text(wanted, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
