# Adding a tool

A tool is a name, a toolset, a JSON schema the model sees and a handler. Built-in tools live under
`src/indus/tools/builtin/`; plugins add tools the same way through their context.

## The handler

```python
from collections.abc import Mapping
from typing import Any

WORD_COUNT_SCHEMA = {
    "name": "word_count",
    "description": "Count the words in a text file. Use it before summarising long files.",
    "parameters": {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "file to count"}},
        "required": ["path"],
    },
}


def word_count(args: Mapping[str, Any]) -> str:
    with open(args["path"], encoding="utf-8") as handle:
        return str(len(handle.read().split()))
```

The handler receives the arguments as a dictionary and returns text (or a multimodal result).
Synchronous handlers run on a worker pool; `async def` handlers run on the event loop. Extra
keyword parameters the handler declares, such as the session's scope, are filled in by the
dispatcher. An exception becomes a tool error the model can read; secrets in it are masked.

## Registering it

In a built-in module, define `register_tools(registry)` and add the module to `BUILTIN_MODULES`
in `src/indus/tools/builtin/__init__.py`:

```python
from indus.tools.registry import ToolRegistry, ToolTraits


def register_tools(registry: ToolRegistry) -> None:
    registry.register(
        "word_count", "file", WORD_COUNT_SCHEMA, word_count, traits=ToolTraits(read_only=True)
    )
```

In a plugin, call `ctx.register_tool(...)` from `register(ctx)` with the same arguments.

Useful options:

- `probe`: a function of the scope that says whether the tool can run here (a key is set, a
  program is installed). Unavailable tools are left out of the session.
- `requires_secrets`: the secret names the tool reads, shown in the reference and in `indus tools`.
- `traits`: `read_only` tools may run in parallel with others; `path_arg` and `writer` let the
  loop know which file a call touches.
- `timeout_s`, `max_result_chars`: limits for one call.

Add the tool to a toolset in `src/indus/tools/toolsets.py` (or register it into an existing one),
then regenerate the reference pages: `python scripts/docs/gen_reference.py`. A test checks that
every registered tool appears in [Tools and toolsets](../reference/tools.md).

## Testing

Call the handler directly for its logic, and run it through `indus.tools.dispatch` with a fake
scope for the approval and policy paths. Never call real services in unit tests; the fakes under
`tests/fakes/` cover models, web providers, backends and platforms.
