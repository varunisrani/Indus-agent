"""Which providers have a documented sign-in flow, and which one ``indus auth add`` uses.

Only flows a provider publishes for third-party programs are listed; nothing here borrows
another product's client id, redirect or identity (R01.12, P2). Each row cites the page it was
checked against.
"""

from __future__ import annotations

from dataclasses import dataclass

from indus.providers.auth.device_code import DeviceFlowSpec
from indus.providers.auth.pkce import PkceFlowSpec


@dataclass(frozen=True)
class SignInFlows:
    provider: str
    pkce: PkceFlowSpec | None = None
    device: DeviceFlowSpec | None = None
    # True only when signing in should be the default type for ``auth add`` (no provider today).
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
}
# fmt: on


def flows_for(provider: str) -> SignInFlows:
    return FLOWS.get(provider, SignInFlows(provider=provider))
