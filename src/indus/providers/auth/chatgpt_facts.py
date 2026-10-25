"""Protocol facts for signing in with a ChatGPT account and calling the ChatGPT Codex backend.

Owner decision D-16.1282 (2026-10-25): Indus signs in through the public OAuth client that OpenAI's
open-source Codex CLI registers, and then sends model calls to the ChatGPT Codex backend. These are
third-party facts, not code; each one cites where it is stated. "Codex CLI" below means
https://github.com/openai/codex (Apache-2.0), read at its ``main`` branch on 2026-10-25:

- ``codex-rs/login/src/server.rs`` — issuer, client id, authorize parameters, the loopback port and
  callback path, the token endpoint and the authorization-code exchange;
- ``codex-rs/login/src/device_code_auth.rs`` — the device-code endpoints, the verification page and
  the redirect URI used when that code is exchanged;
- ``codex-rs/core/src/auth.rs`` and ``codex-rs/login/src/token_data.rs`` — the refresh grant and the
  ``https://api.openai.com/auth`` claim set inside the access token;
- ``codex-rs/core/src/model_provider_info.rs`` and ``codex-rs/core/src/client.rs`` — the backend
  base URL, ``POST {base}/responses`` (streamed, ``store: false``) and the account header;
- ``codex-rs/core/src/models_manager`` — ``GET {base}/models?client_version=…``.

Nothing here is a credential: the client id is public (it ships inside every Codex CLI build).
"""

from __future__ import annotations

from typing import Final

PROVIDER: Final = "openai-codex"
DISPLAY_NAME: Final = "OpenAI (ChatGPT sign-in)"

# -- sign-in (server.rs) -----------------------------------------------------------------------

ISSUER: Final = "https://auth.openai.com"
CLIENT_ID: Final = "app_EMoamEEZ73f0CkXaXp7hrann"  # the Codex CLI's public client id
AUTHORIZE_URL: Final = f"{ISSUER}/oauth/authorize"
TOKEN_URL: Final = f"{ISSUER}/oauth/token"
SCOPE: Final = "openid profile email offline_access"
# The client is registered for exactly this redirect: host spelled ``localhost``, fixed port and
# path. Our listener still binds 127.0.0.1 only; ``localhost`` is just the string in the URI.
CALLBACK_HOST: Final = "localhost"
CALLBACK_PORT: Final = 1455
CALLBACK_PATH: Final = "/auth/callback"
# Extra authorize parameters the Codex CLI sends: ask for the organisations claim in the id token.
AUTHORIZE_EXTRA: Final = {"id_token_add_organizations": "true"}

# -- device code (device_code_auth.rs) ---------------------------------------------------------

DEVICE_USERCODE_URL: Final = f"{ISSUER}/api/accounts/deviceauth/usercode"
DEVICE_TOKEN_URL: Final = f"{ISSUER}/api/accounts/deviceauth/token"
DEVICE_VERIFY_PAGE: Final = f"{ISSUER}/codex/device"
DEVICE_REDIRECT_URI: Final = f"{ISSUER}/deviceauth/callback"
# Status codes the poll answers with while the person has not approved yet.
DEVICE_PENDING_STATUSES: Final = frozenset({403, 404})
# The Codex CLI gives up after fifteen minutes; the server sends ``interval`` (seconds, as text).
DEVICE_DEADLINE_SECONDS: Final = 15 * 60
DEVICE_DEFAULT_INTERVAL: Final = 5
DEVICE_MIN_INTERVAL: Final = 3

# -- tokens (auth.rs, token_data.rs) -----------------------------------------------------------

# The access token is a JWT; this claim object carries the ChatGPT account and plan.
AUTH_CLAIM: Final = "https://api.openai.com/auth"
ACCOUNT_ID_FIELD: Final = "chatgpt_account_id"
PLAN_FIELD: Final = "chatgpt_plan_type"
RESIDENCY_FIELDS: Final = ("chatgpt_data_residency", "chatgpt_compute_residency")

# -- the backend (model_provider_info.rs, client.rs) -------------------------------------------

BACKEND_BASE_URL: Final = "https://chatgpt.com/backend-api/codex"
ACCOUNT_HEADER: Final = "ChatGPT-Account-ID"
RESIDENCY_HEADER: Final = "x-openai-internal-codex-residency"
# Who is calling: the backend asks third-party clients to name themselves. This is ours, never
# the Codex CLI's value (R01.12: requests only identify as this product).
ORIGINATOR_HEADER: Final = "originator"
ORIGINATOR: Final = "indus-agent"
# ``client_version`` is required on the models endpoint; models whose minimal client version is
# newer than it are hidden, so a high version asks for the whole catalogue of the account.
MODELS_CLIENT_VERSION: Final = "1.0.0"

# Model ids offered when the live catalogue cannot be read (offline, expired sign-in). The live
# list (``GET {base}/models``) replaces it whenever it answers.
DEFAULT_MODELS: Final = (
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-6-sol",
    "gpt-6-luna",
)
