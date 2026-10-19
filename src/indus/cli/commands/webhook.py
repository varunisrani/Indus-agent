"""``indus webhook``: routes of the gateway's webhook receiver (P15-32).

``subscribe NAME`` adds or updates a route in ``<home>/gateway/webhook_subscriptions.json``:
``--events a,b`` (event names to take; default all), ``--prompt TEXT`` (template with
``{dot.path}`` keys), ``--deliver TARGET`` (where the reply goes, P14 target grammar),
``--deliver-only`` (send the rendered prompt itself, no agent), ``--auth`` (``auto``,
``github``, ``gitlab``, ``hmac``, ``hmac_ts``), ``--secret-name NAME`` (use a secret you already
stored). Without ``--secret-name`` a new secret is generated, kept in the profile's secret store
as ``INDUS_WEBHOOK_<NAME>`` and printed once; the route file holds only its name.

``list`` (the default) shows subscribed routes and the routes in ``gateway.webhook.routes``;
``remove NAME`` drops a subscribed route (and its generated secret); ``test NAME [--payload
JSON]`` signs a payload with the route's secret and POSTs it to the local receiver.

Exit codes: 0 done, 1 refused or failed (unknown route, bad name, the receiver answered an error
or could not be reached).
"""

from __future__ import annotations

import json
import secrets as pysecrets
from typing import TYPE_CHECKING, Any, cast

import click

if TYPE_CHECKING:
    from indus.foundation.scope import Scope

__all__ = ["webhook_group"]

_SETTINGS = {"help_option_names": ["-h", "--help"], "max_content_width": 100}
_AUTH = ("auto", "github", "gitlab", "hmac", "hmac_ts")
_ALIASES = {"ls": "list", "add": "subscribe", "rm": "remove"}
DEFAULT_PAYLOAD = {"event_type": "test", "message": "Hello from indus webhook test"}


class _Group(click.Group):
    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        return super().get_command(ctx, _ALIASES.get(cmd_name, cmd_name))


def _scope() -> Scope:
    from indus.cli.cmdkit import cli_state

    return cli_state().scope


def _settings(scope: Scope) -> dict[str, Any]:
    from indus.cli.commands._support import fresh_view

    raw = fresh_view(scope).get("gateway.webhook")
    return dict(cast(dict[str, Any], raw)) if isinstance(raw, dict) else {}


def _base_url(settings: dict[str, Any]) -> str:
    host = str(settings.get("host") or "127.0.0.1")
    if host in {"0.0.0.0", "::", ""}:
        host = "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{settings.get('port', 8644)}"


def _fail(message: str) -> None:
    click.echo(f"webhook: {message}", err=True)
    raise click.exceptions.Exit(1)


def _disabled_note(settings: dict[str, Any]) -> None:
    if not settings.get("enabled"):
        click.echo(
            "Note: the webhook receiver is off; set gateway.webhook.enabled to true and restart "
            "the gateway to receive events.",
            err=True,
        )


@click.group("webhook", cls=_Group, context_settings=_SETTINGS, invoke_without_command=True)
@click.pass_context
def webhook_group(ctx: click.Context) -> None:
    """Add, list, remove and test webhook routes."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(list_routes)


@webhook_group.command("subscribe", context_settings=_SETTINGS)
@click.argument("name")
@click.option("--events", default="", help="Comma-separated event names to take (default: all).")
@click.option("--prompt", default="", help="Prompt template; {dot.path} reads the payload.")
@click.option("--deliver", default="", help="Where the reply goes (for example telegram:123).")
@click.option("--deliver-only", is_flag=True, help="Send the rendered prompt itself; no agent.")
@click.option(
    "--auth",
    type=click.Choice(_AUTH),
    default="auto",
    show_default=True,
    help="How the sender signs requests.",
)
@click.option("--secret-name", default="", help="Name of a secret you already stored.")
@click.option("--description", default="", help="A note shown by `list`.")
def subscribe(
    name: str,
    events: str,
    prompt: str,
    deliver: str,
    deliver_only: bool,
    auth: str,
    secret_name: str,
    description: str,
) -> None:
    """Add or update a subscribed route."""
    from indus.cli.commands._support import secret_store
    from indus.state.webhook_subscriptions import (
        ROUTE_NAME_RE,
        SubscriptionStore,
        normalise_name,
        secret_name_for,
    )

    scope = _scope()
    route_name = normalise_name(name)
    if not ROUTE_NAME_RE.match(route_name):
        _fail(f"invalid name {name!r}: use lower-case letters, digits, '-' and '_'")
    if deliver_only and not deliver.strip():
        _fail("--deliver-only needs --deliver with a real target")
    store = SubscriptionStore(scope)
    existing = store.load().get(route_name, {})
    vault = secret_store(scope)
    generated: str | None = None
    if secret_name.strip():
        chosen = secret_name.strip()
        if not (vault.get(chosen) or scope.env.get(chosen)):
            _fail(f"no stored secret named {chosen} (store one first)")
    else:
        chosen = str(existing.get("secret") or secret_name_for(route_name))
        if not vault.get(chosen):
            generated = pysecrets.token_urlsafe(32)
            vault.set(chosen, generated)
    route: dict[str, Any] = {
        "secret": chosen,
        "auth": auth,
        "events": [e.strip() for e in events.split(",") if e.strip()],
        "prompt": prompt,
        "deliver": deliver.strip() or None,
        "deliver_only": deliver_only,
        "description": description or f"Subscribed with indus webhook: {route_name}",
        "created_at": str(
            existing.get("created_at") or scope.clock.now().isoformat(timespec="seconds")
        ),
    }
    new = store.put(route_name, route)
    settings = _settings(scope)
    click.echo(f"{'Created' if new else 'Updated'} webhook route {route_name}.")
    click.echo(f"  URL:     {_base_url(settings)}/webhooks/{route_name}")
    click.echo(f"  Auth:    {auth}")
    click.echo(f"  Events:  {', '.join(route['events']) or '(all)'}")
    target = route["deliver"] or "(kept in the session)"
    click.echo(f"  Deliver: {target}" + ("  (direct, no agent)" if deliver_only else ""))
    click.echo(f"  Secret:  stored as {chosen}")
    if generated is not None:
        click.echo("")
        click.echo(f"  {generated}")
        click.echo("")
        click.echo(
            "This is the signing secret; copy it into the sender now. It is not shown again."
        )
    _disabled_note(settings)


def _all_routes(scope: Scope) -> dict[str, tuple[str, dict[str, Any]]]:
    from indus.state.webhook_subscriptions import SubscriptionStore

    found = {n: ("subscribed", r) for n, r in SubscriptionStore(scope).load().items()}
    configured = _settings(scope).get("routes")
    if isinstance(configured, dict):
        for name, raw in cast(dict[str, Any], configured).items():
            if isinstance(raw, dict):
                found[str(name)] = ("config", cast(dict[str, Any], raw))
    return found


@webhook_group.command("list", context_settings=_SETTINGS)
@click.option("--json", "as_json", is_flag=True, help="Print JSON.")
def list_routes(as_json: bool) -> None:
    """Show the receiver's routes (never a secret value)."""
    scope = _scope()
    settings = _settings(scope)
    routes = _all_routes(scope)
    if as_json:
        rows = [
            {
                "name": name,
                "source": source,
                "url": f"{_base_url(settings)}/webhooks/{name}",
                "auth": route.get("auth", "auto"),
                "insecure": bool(route.get("insecure")),
                "events": route.get("events") or [],
                "deliver": route.get("deliver"),
                "deliver_only": bool(route.get("deliver_only")),
                "secret": route.get("secret"),
            }
            for name, (source, route) in sorted(routes.items())
        ]
        click.echo(json.dumps({"routes": rows}, indent=2))
        return
    if not routes:
        click.echo("No webhook routes. Add one with `indus webhook subscribe NAME`.")
        _disabled_note(settings)
        return
    click.echo(f"Webhook routes ({len(routes)}):")
    for name, (source, route) in sorted(routes.items()):
        events = ", ".join(route.get("events") or []) or "(all)"
        deliver = route.get("deliver") or "(kept in the session)"
        if route.get("deliver_only"):
            deliver += " (direct, no agent)"
        click.echo(f"  {name}  [{source}]")
        if route.get("description"):
            click.echo(f"    {route['description']}")
        click.echo(f"    URL:     {_base_url(settings)}/webhooks/{name}")
        click.echo(
            f"    Auth:    {'insecure' if route.get('insecure') else route.get('auth', 'auto')}"
        )
        click.echo(f"    Events:  {events}")
        click.echo(f"    Deliver: {deliver}")
    _disabled_note(settings)


@webhook_group.command("remove", context_settings=_SETTINGS)
@click.argument("name")
def remove(name: str) -> None:
    """Drop a subscribed route (config routes are edited in the config file)."""
    from indus.cli.commands._support import secret_store
    from indus.state.webhook_subscriptions import (
        SubscriptionStore,
        normalise_name,
        secret_name_for,
    )

    scope = _scope()
    route_name = normalise_name(name)
    gone = SubscriptionStore(scope).remove(route_name)
    if gone is None:
        _fail(f"no subscribed route named {route_name!r} (config routes live in the config file)")
        return
    if gone.get("secret") == secret_name_for(route_name):
        secret_store(scope).delete(secret_name_for(route_name))
    click.echo(f"Removed webhook route {route_name}.")


@webhook_group.command("test", context_settings=_SETTINGS)
@click.argument("name")
@click.option("--payload", default="", help="JSON to send (default: a small test event).")
@click.option("--timeout", default=10.0, show_default=True, help="Seconds to wait.")
def test(name: str, payload: str, timeout: float) -> None:
    """Sign a test payload with the route's secret and POST it to the local receiver."""
    import httpx

    from indus.cli.commands._support import secret_store
    from indus.state.webhook_subscriptions import normalise_name, sign_request

    scope = _scope()
    route_name = normalise_name(name)
    found = _all_routes(scope).get(route_name)
    if found is None:
        _fail(f"no route named {route_name!r}")
        return
    route = found[1]
    if payload:
        try:
            json.loads(payload)
        except ValueError:
            _fail("--payload is not valid JSON")
        body = payload.encode("utf-8")
    else:
        body = json.dumps(DEFAULT_PAYLOAD).encode("utf-8")
    headers = {"Content-Type": "application/json", "X-Request-Id": pysecrets.token_hex(16)}
    if not route.get("insecure"):
        secret_ref = str(route.get("secret") or "")
        value = secret_store(scope).get(secret_ref) if secret_ref else None
        value = value or (scope.env.get(secret_ref) if secret_ref else None)
        if not value:
            _fail(f"the route's secret {secret_ref or '(none)'} is not stored")
            return
        auth = str(route.get("auth") or "auto")
        headers.update(sign_request(auth, value, body, int(scope.clock.now().timestamp())))
        if auth == "github":
            headers["X-GitHub-Event"] = "test"
    url = f"{_base_url(_settings(scope))}/webhooks/{route_name}"
    click.echo(f"Sending a test event to {url}")
    try:
        with httpx.Client(timeout=timeout, trust_env=False) as client:
            answer = client.post(url, content=body, headers=headers)
    except httpx.HTTPError as exc:
        _fail(f"could not reach the receiver ({type(exc).__name__}); is the gateway running?")
        return
    click.echo(f"Answer {answer.status_code}: {answer.text.strip()[:500]}")
    if answer.status_code >= 400:
        raise click.exceptions.Exit(1)
