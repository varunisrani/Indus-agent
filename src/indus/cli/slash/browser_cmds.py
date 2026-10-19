"""``/browser``: which browser the browser tools drive for this profile (C26).

``/browser connect [<cdp-url>]`` attaches the tools to a Chromium-family browser that is already
running with remote debugging (default ``http://127.0.0.1:9222``); the address must answer first,
otherwise nothing changes. ``/browser disconnect`` drops that choice (back to
``INDUS_BROWSER_CDP_URL`` / ``browser.cdp_url`` when set, else the local headless Chromium).
``/browser use local`` forces the local headless Chromium even when an endpoint is configured.
``/browser status`` (or no argument) shows the current choice.

The choice is kept per profile in the process (``SessionManager.choices``); open browser sessions
notice it on their next call and reconnect. ``browser_cdp`` joins the tool array of sessions that
start after a live endpoint is chosen (the tool array of a running session does not change).
"""

from __future__ import annotations

from indus.cli.slash.context import Handler, SlashContext, SlashResult
from indus.tools.builtin import browser as browser_tools
from indus.tools.builtin.browser import cdp
from indus.tools.builtin.browser.driver import playwright_available
from indus.tools.builtin.browser.session import Choice, configured_endpoint

__all__ = ["DEFAULT_ENDPOINT", "HANDLERS", "USAGE", "cmd_browser"]

USAGE = "Usage: /browser [connect <cdp-url> | disconnect | status | use local]"
DEFAULT_ENDPOINT = "http://127.0.0.1:9222"
ENDPOINT_HINT = (
    "Start Chrome or Chromium with --remote-debugging-port=9222 (and a separate "
    "--user-data-dir), then run /browser connect again."
)


def _tools() -> browser_tools.BrowserTools:
    return browser_tools.DEFAULT_TOOLS


def _status(ctx: SlashContext) -> None:
    tools = _tools()
    manager = tools.manager
    choice = manager.choice_for(ctx.scope)
    if choice.mode == "cdp" and choice.cdp_url:
        where = {
            "connect": "/browser connect",
            "env": "INDUS_BROWSER_CDP_URL",
            "config": "browser.cdp_url",
        }.get(choice.source, choice.source)
        live = cdp.endpoint_live(choice.cdp_url)
        ctx.say(
            f"Browser: attached over CDP to {cdp.safe_endpoint(choice.cdp_url, ctx.scope)} "
            f"(set by {where}); " + ("it answers." if live else "it does not answer now.")
        )
    else:
        how = " (forced by /browser use local)" if choice.source == "use-local" else ""
        ready = "" if playwright_available() else " The browser extra is not installed."
        ctx.say(f"Browser: local headless Chromium{how}.{ready}")
    home = ctx.scope.home
    count = sum(1 for s in manager.sessions.values() if s.scope.home == home)
    ctx.say(f"Open browser sessions for this profile: {count}.")


def _connect(ctx: SlashContext, words: list[str]) -> None:
    if len(words) > 1:
        ctx.say(USAGE)
        return
    url = cdp.normalise_endpoint(words[0] if words else DEFAULT_ENDPOINT)
    if url is None:
        ctx.say(f"Not a CDP address: {words[0] if words else ''!r}. {USAGE}")
        return
    shown = cdp.safe_endpoint(url, ctx.scope)
    if not cdp.endpoint_live(url):
        ctx.say(f"Nothing answers at {shown}; the browser tools were not changed.", ENDPOINT_HINT)
        return
    _tools().manager.choices.set(ctx.scope, Choice("cdp", url, "connect"))
    ctx.say(
        f"The browser tools now drive the browser at {shown}.",
        "Open browser sessions switch on their next call; browser_cdp is offered to sessions "
        "started from now on. The agent works in its own tab, but pages and sign-ins of that "
        "browser are visible to it.",
    )


def _disconnect(ctx: SlashContext) -> None:
    choices = _tools().manager.choices
    if choices.get(ctx.scope) is None:
        ctx.say("The browser tools are not connected with /browser connect; nothing to undo.")
        return
    choices.set(ctx.scope, None)
    fallback = configured_endpoint(ctx.scope)
    if fallback is not None:
        ctx.say(f"Disconnected. The configured endpoint applies again: {fallback[0]}.")
    else:
        ctx.say("Disconnected. The browser tools use the local headless Chromium again.")


def cmd_browser(ctx: SlashContext, args: str) -> SlashResult:
    words = args.split()
    word = words[0].lower() if words else "status"
    rest = words[1:]
    if word == "status" and not rest:
        _status(ctx)
    elif word == "connect":
        _connect(ctx, rest)
    elif word == "disconnect" and not rest:
        _disconnect(ctx)
    elif word == "use" and [w.lower() for w in rest] == ["local"]:
        _tools().manager.choices.set(ctx.scope, Choice("local", None, "use-local"))
        ctx.say(
            "The browser tools use the local headless Chromium (configured endpoints are "
            "ignored until /browser disconnect)."
        )
    else:
        ctx.say(USAGE)
    return SlashResult()


HANDLERS: dict[str, Handler] = {"browser": cmd_browser}
