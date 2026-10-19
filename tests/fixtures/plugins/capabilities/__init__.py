"""Probes its grants and only registers what it was given."""


def register(ctx):
    if ctx.has_capability("platform.actions"):
        ctx.register_command("act", lambda raw_args: "acted")
