"""Replaces a built-in tool when the operator allows it."""

SCHEMA = {"description": "A replacement reader.", "parameters": {"type": "object"}}


def reader(args, **_):
    return "replaced"


def register(ctx):
    ctx.register_tool("read_file", "file", SCHEMA, reader, override=True)
