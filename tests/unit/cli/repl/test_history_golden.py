"""The classic CLI reads and writes the history file the TUI shares (P12-17 golden)."""

from __future__ import annotations

from pathlib import Path

from indus.cli.repl.history import InputHistory

GOLDEN = Path(__file__).resolve().parents[3] / "fixtures" / "tui" / "input_history.golden"
ENTRIES = [
    "first prompt",
    "explain this\nacross two lines",
    "unicode: café 漢字 👍🏽",
    'quote "inside" and back\\slash',
    "last one",
]


def test_golden_history_loads_in_the_classic_cli() -> None:
    history = InputHistory(GOLDEN)
    assert list(history.load_history_strings()) == list(reversed(ENTRIES))


def test_classic_cli_writes_the_golden_bytes(tmp_path: Path) -> None:
    path = tmp_path / "input_history"
    history = InputHistory(path)
    for entry in ENTRIES:
        history.store_string(entry)
    assert path.read_bytes() == GOLDEN.read_bytes()
