"""What the ``openai-codex`` route reads from a ChatGPT sign-in's token, and its request shape.

Kept free of HTTP and sign-in machinery (standard library only) because the bundled provider
catalogue imports it at start-up. The sign-in flows themselves are in ``auth.chatgpt``.

- **Claims.** The access token is a JWT whose ``https://api.openai.com/auth`` object names the
  ChatGPT account (sent back as the account header) and the plan (shown in status). It is read
  without verification, only for those two uses; the backend is the one that checks it.
- **Requests.** The backend takes the Responses shape, streamed and unstored, without an output
  cap or sampling fields, and always with ``instructions`` (``prepare_body``, the profile's hook).
- **Models.** ``GET {base}/models?client_version=…`` lists the account's models (``slug``,
  ``visibility``, ``priority``; ``model_list.parse_models`` reads that shape). Without an answer
  the profile's documented ``default_models`` stand in.
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any, cast

from indus.providers.auth import chatgpt_facts as facts

# -- claims --------------------------------------------------------------------------------------


def claims(access_token: str) -> dict[str, Any]:
    """The JWT payload of ``access_token`` (unverified); ``{}`` for anything that is not one."""
    parts = access_token.split(".") if isinstance(access_token, str) else []  # pyright: ignore[reportUnnecessaryIsInstance]
    if len(parts) != 3:
        return {}
    body = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(body.encode("ascii")))
    except (ValueError, UnicodeError, binascii.Error):
        return {}
    return cast(dict[str, Any], data) if isinstance(data, dict) else {}


def _auth_claims(access_token: str) -> dict[str, Any]:
    found = claims(access_token).get(facts.AUTH_CLAIM)
    return cast(dict[str, Any], found) if isinstance(found, dict) else {}


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def account_id(access_token: str) -> str:
    return _text(_auth_claims(access_token).get(facts.ACCOUNT_ID_FIELD))


def plan(access_token: str) -> str | None:
    return _text(_auth_claims(access_token).get(facts.PLAN_FIELD)) or None


def account_headers(access_token: str) -> dict[str, str]:
    """The account (and data-residency) headers the backend reads; ``{}`` for a token without
    them (the backend then answers 401, which the pool treats as a sign-in problem)."""
    auth = _auth_claims(access_token)
    headers: dict[str, str] = {}
    account = _text(auth.get(facts.ACCOUNT_ID_FIELD))
    if account and "\n" not in account and "\r" not in account:
        headers[facts.ACCOUNT_HEADER] = account
    for field_name in facts.RESIDENCY_FIELDS:
        residency = _text(auth.get(field_name))
        if residency and "\n" not in residency and "\r" not in residency:
            headers[facts.RESIDENCY_HEADER] = residency
            break
    return headers


# -- the backend's request shape ----------------------------------------------------------------

# Request fields the backend refuses (it sets the output budget and sampling itself).
DROPPED_FIELDS = ("max_output_tokens", "temperature", "top_p")
DEFAULT_INSTRUCTIONS = "Follow the instructions in the conversation."


def prepare_body(body: dict[str, Any], model: str) -> dict[str, Any]:
    """The ``openai-codex`` profile's body hook over the Responses body: always streamed and not
    stored, never an output cap or sampling field, and always an ``instructions`` text."""
    out = {k: v for k, v in body.items() if k not in DROPPED_FIELDS}
    out["store"] = False
    out["stream"] = True
    if not isinstance(out.get("instructions"), str) or not out["instructions"].strip():
        out["instructions"] = DEFAULT_INSTRUCTIONS
    return out


# -- model list ----------------------------------------------------------------------------------


def models_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/models?client_version={facts.MODELS_CLIENT_VERSION}"
