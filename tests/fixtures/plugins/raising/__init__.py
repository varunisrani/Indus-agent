"""register() adds a hook and then fails."""


def observe(**_):
    return None


def register(ctx):
    ctx.register_hook("post_tool_call", observe)
    raise RuntimeError("sample failure while registering")
