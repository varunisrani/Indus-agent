"""A scriptable MCP server for tests, on the SDK's server side.

Run as a script (``python stdio_server.py [flags]``) it serves stdio. ``build_server`` gives the
same tools to the in-process HTTP fake. Flags:

- ``--name NAME``          server name (default ``fake``)
- ``--hang``               never answer anything (reads stdin until it closes)
- ``--stderr TEXT``        write TEXT to stderr at start
- ``--crash-on-start``     exit with status 3 right after the stderr text
- ``--pidfile PATH``       write this process's pid
- ``--grandchild PATH``    start a long sleeping child process and write its pid

Tools: ``echo`` (read-only hint), ``add``, ``getenv`` (a variable of this process or
``<unset>``), ``cwd``, ``crash`` (the process exits at once).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

UNSET = "<unset>"


def build_server(name: str = "fake") -> MCPServer:
    server = MCPServer(name, log_level="WARNING")

    @server.tool(annotations=ToolAnnotations(read_only_hint=True))
    def echo(text: str) -> str:
        """Return the text unchanged."""
        return text

    @server.tool()
    def add(a: int, b: int) -> int:
        """Add two whole numbers."""
        return a + b

    @server.tool()
    def getenv(name: str) -> str:
        """The value of one environment variable of the server process."""
        return os.environ.get(name, UNSET)

    @server.tool()
    def cwd() -> str:
        """The server's working directory."""
        return os.getcwd()

    @server.tool()
    def crash() -> str:
        """Stop the server process at once."""
        os._exit(7)

    return server


def _hang() -> None:
    while sys.stdin.buffer.read(4096):
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="fake")
    parser.add_argument("--hang", action="store_true")
    parser.add_argument("--stderr", default="")
    parser.add_argument("--crash-on-start", action="store_true")
    parser.add_argument("--pidfile", default="")
    parser.add_argument("--grandchild", default="")
    args = parser.parse_args(argv)
    if args.pidfile:
        with open(args.pidfile, "w", encoding="utf-8") as out:
            out.write(str(os.getpid()))
    if args.stderr:
        sys.stderr.write(args.stderr + "\n")
        sys.stderr.flush()
    if args.crash_on_start:
        return 3
    if args.grandchild:
        child = subprocess.Popen(
            [sys.executable, "-c", "import time\nwhile True: time.sleep(1)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        with open(args.grandchild, "w", encoding="utf-8") as out:
            out.write(str(child.pid))
    if args.hang:
        _hang()
        return 0
    build_server(args.name).run("stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
