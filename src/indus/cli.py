"""Command-line entry point for `indus` (placeholder until the CLI phase)."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from indus import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="indus", description="Indus Agent command line.")
    parser.add_argument("--version", action="version", version=f"indus {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    build_parser().parse_args(argv)
    print("indus: not implemented yet; only --version works in this build.")
    return 0
