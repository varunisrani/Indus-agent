"""One way to run a device sign-in and to renew its tokens, whichever kind the provider has.

Two kinds exist: RFC 8628 (``device_code.DeviceFlowSpec``) and OpenAI's ChatGPT device flow
(``chatgpt.ChatGptDeviceSpec``). ``start`` asks for the code to show, ``finish`` waits for the
person's approval and returns the tokens, ``run_login`` does both and talks to the person.
``refresh_spec`` is the token endpoint that renews a provider's stored sign-in, from its flows row.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

from indus.foundation.clock import Clock
from indus.providers.auth import chatgpt, device_code
from indus.providers.auth.device_code import (
    DeviceAuthorization,
    DeviceFlowSpec,
    OpenUrl,
    Say,
    TokenSet,
)

if TYPE_CHECKING:
    from indus.providers.auth.flows import SignInFlows

type DeviceSpec = DeviceFlowSpec | chatgpt.ChatGptDeviceSpec


async def start(
    client: httpx.AsyncClient, spec: DeviceSpec, *, clock: Clock
) -> DeviceAuthorization:
    if isinstance(spec, chatgpt.ChatGptDeviceSpec):
        return await chatgpt.request_user_code(client, spec, clock=clock)
    return await device_code.request_device_authorization(client, spec)


async def finish(
    client: httpx.AsyncClient, spec: DeviceSpec, grant: DeviceAuthorization, *, clock: Clock
) -> TokenSet:
    if isinstance(spec, chatgpt.ChatGptDeviceSpec):
        return await chatgpt.wait_for_approval(client, spec, grant, clock=clock)
    return await device_code.poll_for_token(client, spec, grant, clock=clock)


async def run_login(
    spec: DeviceSpec,
    *,
    clock: Clock,
    say: Say,
    open_url: OpenUrl | None,
    client: httpx.AsyncClient,
) -> TokenSet:
    """Ask for a code, show it, open the page when ``open_url`` is given, wait for approval."""
    if isinstance(spec, DeviceFlowSpec):
        return await device_code.run_device_login(
            spec, clock=clock, say=say, open_url=open_url, client=client
        )
    grant = await start(client, spec, clock=clock)
    say("To sign in with your ChatGPT account:")
    say(f"  1. Open {grant.url_to_show}")
    say(f"  2. Enter the code {grant.user_code}")
    say(f"The code is valid for about {max(1, round(grant.expires_in / 60))} minute(s).")
    if open_url is not None:
        if open_url(grant.url_to_show):
            say("(A browser window was opened for you.)")
        else:
            say("(No browser could be opened; use the address above.)")
    say("Waiting for approval… (Ctrl+C cancels)")
    return await finish(client, spec, grant, clock=clock)


def refresh_spec(provider: str) -> DeviceFlowSpec | None:
    """The token endpoint that renews ``provider``'s stored sign-in; None when it has none."""
    from indus.providers.auth.flows import flows_for

    return refresh_spec_of(flows_for(provider))


def refresh_spec_of(flows: SignInFlows) -> DeviceFlowSpec | None:
    """``refresh_spec`` from a flows row: the device row's token endpoint, else the browser row's
    authorization-code exchange (a key mint has nothing to renew)."""
    if isinstance(flows.device, DeviceFlowSpec):
        return flows.device
    if isinstance(flows.device, chatgpt.ChatGptDeviceSpec):
        return DeviceFlowSpec(
            provider=flows.provider,
            device_authorization_url="",
            token_url=flows.device.token_url,
            client_id=flows.device.client_id,
        )
    if flows.pkce is not None and flows.pkce.exchange == "token":
        return DeviceFlowSpec(
            provider=flows.provider,
            device_authorization_url="",
            token_url=flows.pkce.exchange_url,
            client_id=flows.pkce.client_id,
            scope=flows.pkce.scope,
        )
    return None
