"""Which providers have a sign-in flow, and which one ``indus auth add`` uses.

Flows a provider publishes for third-party programs are listed with the page they were checked
against. One row is the owner's explicit exception (D-16.1282, 2026-10-25): ``openai-codex`` signs
in with a ChatGPT account through the public client of OpenAI's open-source Codex CLI (facts and
citations in ``chatgpt_facts``). No other row borrows another product's client id or redirect,
and no row ever reads another program's saved login (R01.12, P2).
"""

from __future__ import annotations

from dataclasses import dataclass

from indus.providers.auth import chatgpt_facts
from indus.providers.auth.chatgpt import ChatGptDeviceSpec
from indus.providers.auth.device_signin import DeviceSpec
from indus.providers.auth.pkce import PkceFlowSpec


@dataclass(frozen=True)
class SignInFlows:
    provider: str
    pkce: PkceFlowSpec | None = None
    device: DeviceSpec | None = None
    # True when signing in is the default type for ``auth add`` (a provider without API keys).
    oauth_default: bool = False

    @property
    def any(self) -> bool:
        return self.pkce is not None or self.device is not None


# fmt: off
FLOWS: dict[str, SignInFlows] = {
    # https://openrouter.ai/docs/guides/overview/auth/oauth (checked 2026-10-18): PKCE key mint;
    # localhost callbacks on any port, `state` echoed, code valid 10 minutes, no client id.
    # The minted value is an ordinary API key, so keys stay the default type.
    "openrouter": SignInFlows(
        provider="openrouter",
        pkce=PkceFlowSpec(
            provider="openrouter",
            authorize_url="https://openrouter.ai/auth",
            exchange_url="https://openrouter.ai/api/v1/auth/keys",
            exchange="key_mint",
            extra_params={"key_label": "Indus Agent"},
            doc_url="https://openrouter.ai/docs/guides/overview/auth/oauth",
        ),
    ),
    # D-16.1282: ChatGPT sign-in. Browser (authorization code + PKCE on the client's registered
    # loopback redirect) by default; the device code when asked for, or when port 1455 is busy.
    chatgpt_facts.PROVIDER: SignInFlows(
        provider=chatgpt_facts.PROVIDER,
        pkce=PkceFlowSpec(
            provider=chatgpt_facts.PROVIDER,
            authorize_url=chatgpt_facts.AUTHORIZE_URL,
            exchange_url=chatgpt_facts.TOKEN_URL,
            exchange="token",
            client_id=chatgpt_facts.CLIENT_ID,
            scope=chatgpt_facts.SCOPE,
            fixed_port=chatgpt_facts.CALLBACK_PORT,
            extra_params=chatgpt_facts.AUTHORIZE_EXTRA,
            fallback_device=ChatGptDeviceSpec(),
            doc_url="https://github.com/openai/codex (codex-rs/login/src/server.rs)",
            redirect_host=chatgpt_facts.CALLBACK_HOST,
            callback_path=chatgpt_facts.CALLBACK_PATH,
        ),
        device=ChatGptDeviceSpec(),
        oauth_default=True,
    ),
}
# fmt: on


def flows_for(provider: str) -> SignInFlows:
    return FLOWS.get(provider, SignInFlows(provider=provider))
