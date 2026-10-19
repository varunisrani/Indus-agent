"""A sample plugin that registers one thing of every kind."""

from pathlib import Path

SCHEMA = {
    "description": "Repeat the given text.",
    "parameters": {"type": "object", "properties": {"text": {"type": "string"}}},
}


def echo(args, **_):
    return "echo: " + str(args.get("text", ""))


def watch(tool_name, **_):
    return None


def hello(raw_args, **_):
    return "hello " + raw_args


class Speaker:
    name = "sample-voice"


def section(info):
    return "Sample plugin notes."


def heard(**payload):
    return None


def register(ctx):
    ctx.register_tool("sample_echo", "sample", SCHEMA, echo)
    ctx.register_hook("post_tool_call", watch)
    ctx.register_command("hello", hello, description="say hello")
    ctx.register_skill("greet", Path(__file__).parent / "skills" / "greet")
    ctx.register_tts_provider(Speaker())
    ctx.register_prompt_section("sample.notes", section)
    ctx.listen("indus.session_started", heard)
