"""Seeded random transcripts for the compression tests (valid alternation by construction)."""

from __future__ import annotations

import json
import random

from indus.core.transcript import TranscriptRow


def call(ident: str, tool: str = "read_file", **args: object) -> dict[str, object]:
    return {
        "id": ident,
        "type": "function",
        "function": {"name": tool, "arguments": json.dumps(args or {"path": "src/app.py"})},
    }


def conversation(
    rng: random.Random, exchanges: int, *, big: int = 12_000, pending: bool = False
) -> list[TranscriptRow]:
    rows = [TranscriptRow(role="system", content="You are a test agent.")]
    serial = 0
    for turn in range(exchanges):
        rows.append(
            TranscriptRow(role="user", content=f"request {turn}: " + "w" * rng.randint(5, 400))
        )
        for _ in range(rng.randint(0, 3)):
            calls = []
            for _ in range(rng.randint(1, 3)):
                serial += 1
                calls.append(call(f"c{serial}"))
            rows.append(TranscriptRow(role="assistant", content="", tool_calls=calls))
            for c in calls:
                rows.append(
                    TranscriptRow(
                        role="tool",
                        name="read_file",
                        tool_call_id=str(c["id"]),
                        content="r" * rng.randint(10, big),
                    )
                )
        rows.append(TranscriptRow(role="assistant", content="answer " + "a" * rng.randint(5, 600)))
    if pending:
        serial += 1
        c = call(f"c{serial}")
        rows.append(TranscriptRow(role="assistant", content="", tool_calls=[c]))
        rows.append(
            TranscriptRow(
                role="tool", name="read_file", tool_call_id=str(c["id"]), content="p" * 5_000
            )
        )
    return rows
