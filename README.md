# Indus Agent

Indus Agent is an AI agent that gets better the longer you use it. It works in your terminal and in
chat apps, runs tools on your behalf, remembers what it learns across sessions, and turns solved
problems into reusable skills.

## Status

Pre-alpha. A clean-room rebuild is in progress: nothing beyond `indus --version` works yet. The plan
lives in [`docs/plan/`](docs/plan/README.md); the rules for contributors (people and agents) are in
[`AGENTS.md`](AGENTS.md).

## Development quickstart

Requires [uv](https://docs.astral.sh/uv/) (Python 3.12+; the repo pins 3.13).

```bash
uv sync                                          # create .venv with the dev tools
uv run indus --version                           # prints "indus 0.0.1"
uv run pytest                                    # tests
uv run python -m tooling.checks --require-ref    # clean-room guards (needs the reference, see AGENTS.md)
```

## Credits

Indus Agent's design is inspired by hermes-agent by Nous Research (MIT). Indus Agent is written from
scratch. See [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).
