# CLAUDE.md

See [`AGENTS.md`](AGENTS.md): its rules (clean room, names, task notes, commits, definition of done)
apply to Claude Code in full.

Commands:

```bash
uv sync
uv run ruff check . && uv run ruff format --check .
uv run pytest -q
uv run python -m tooling.checks --require-ref
```
