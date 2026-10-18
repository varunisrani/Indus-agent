<!-- Both lines below are required; CI fails without them. -->
Reference: <reference files read at ab3fe9d, and Evidence ids; or "none" and why>
Task note: docs/notes/<phase>/<slug>.md

## Summary

- <what changed and why>

## Tests

- [ ] `uv run ruff check .` and `uv run ruff format --check .`
- [ ] `uv run pytest -q` (<counts>)
- [ ] `uv run python -m tooling.checks --require-ref`
- <new or changed tests>
