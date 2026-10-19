"""register() blocks until the event in the ``hold`` setting is set."""


def register(ctx):
    started = ctx.setting("started")
    if started is not None:
        started.set()
    ctx.setting("hold").wait()
    ctx.register_command("late", lambda raw_args: "too late")
