"""A stdio MCP server whose tools ask the client things (P13-10 tests).

- ``summarize(text)``: sends ``sampling/createMessage`` (hint ``small-model``, 10,000 tokens
  asked) and returns ``<model>|<stop reason>|<text>``;
- ``signup()``: sends a form ``elicitation/create`` for a contact and returns
  ``<action>|<email>|<age>|<subscribe>``;
- ``open_page()``: sends a URL-mode elicitation and returns the action.

Run as a script; it serves stdio.
"""

from __future__ import annotations

import sys
import warnings

import mcp_types as types
from mcp.server.mcpserver import Context, MCPServer
from pydantic import BaseModel

warnings.simplefilter("ignore")  # sampling is marked deprecated in newer protocol revisions


class Contact(BaseModel):
    email: str
    age: int
    subscribe: bool


def build_server() -> MCPServer:
    server = MCPServer("asking", log_level="WARNING")

    @server.tool()
    async def summarize(text: str, ctx: Context) -> str:  # pyright: ignore[reportMissingTypeArgument]
        """Ask the client's model to summarise the text."""
        result = await ctx.session.create_message(
            messages=[
                types.SamplingMessage(
                    role="user", content=types.TextContent(type="text", text=text)
                )
            ],
            max_tokens=10_000,
            model_preferences=types.ModelPreferences(hints=[types.ModelHint(name="small-model")]),
            related_request_id=ctx.request_id,
        )
        content = result.content
        text_out = content.text if isinstance(content, types.TextContent) else "?"
        return f"{result.model}|{result.stop_reason}|{text_out}"

    @server.tool()
    async def signup(ctx: Context) -> str:  # pyright: ignore[reportMissingTypeArgument]
        """Ask the person for their contact details."""
        answer = await ctx.elicit("Who should we register?", schema=Contact)
        data = answer.data if answer.action == "accept" else None
        if data is None:
            return f"{answer.action}|||"
        return f"{answer.action}|{data.email}|{data.age}|{data.subscribe}"

    @server.tool()
    async def open_page(ctx: Context) -> str:  # pyright: ignore[reportMissingTypeArgument]
        """Ask the person to open a page."""
        answer = await ctx.elicit_url("Please sign in", "https://example.test/login", "e-1")
        return answer.action

    return server


if __name__ == "__main__":
    build_server().run("stdio")
    sys.exit(0)
