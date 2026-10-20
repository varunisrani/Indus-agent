"""An in-process OpenID Connect provider for the dashboard's OIDC tests (P18-04).

``FakeIdp`` is a Starlette app with discovery, a JWKS, an authorization endpoint (it answers at
once with a redirect carrying a code, as if the person had signed in), a token endpoint
(authorization code with PKCE S256 checked, refresh token grant) and RFC 7009 revocation. Keys are
generated when the fake is built (RSA 2048 and EC P-256); nothing is stored on disk. ``client()``
is an ``httpx.Client`` (Starlette's TestClient) that routes every URL to the fake, whatever the
host, so the provider under test talks HTTPS URLs without any network.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode, urlsplit

import jwt
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from jwt.algorithms import ECAlgorithm, RSAAlgorithm
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.testclient import TestClient

ISSUER = "https://idp.example.test/realms/indus"
CLIENT_ID = "indus-dashboard"


def _s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


@dataclass
class _Code:
    challenge: str
    redirect_uri: str
    nonce: str
    claims: dict[str, Any]


@dataclass
class FakeIdp:
    issuer: str = ISSUER
    client_id: str = CLIENT_ID
    advertised_issuer: str | None = None
    """What discovery claims the issuer is (None = the real one)."""
    revocation: bool = True
    expires_in: int = 300
    user: dict[str, Any] = field(
        default_factory=lambda: {
            "sub": "user-1",
            "email": "ada@example.test",
            "name": "Ada",
            "groups": ["ops", "dev"],
        }
    )
    refresh_gate: threading.Event | None = None
    """When set, the refresh grant waits for it (to hold concurrent refreshes together)."""
    refresh_on_id_token: bool = True

    def __post_init__(self) -> None:
        self.rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.ec_key = ec.generate_private_key(ec.SECP256R1())
        self.codes: dict[str, _Code] = {}
        self.refresh_tokens: dict[str, dict[str, Any]] = {}
        self.revoked: list[str] = []
        self.grant_calls = 0
        self.refresh_calls = 0
        self._lock = threading.Lock()
        base = urlsplit(self.issuer).path.rstrip("/")
        self.app = Starlette(
            routes=[
                Route(f"{base}/.well-known/openid-configuration", self._discovery),
                Route(f"{base}/jwks", self._jwks),
                Route(f"{base}/authorize", self._authorize),
                Route(f"{base}/token", self._token, methods=["POST"]),
                Route(f"{base}/revoke", self._revoke, methods=["POST"]),
            ]
        )

    # -- helpers for tests --

    def client(self) -> TestClient:
        return TestClient(self.app, base_url=self.issuer, follow_redirects=False)

    def jwk_set(self) -> dict[str, Any]:
        rsa_jwk: dict[str, Any] = RSAAlgorithm.to_jwk(self.rsa_key.public_key(), as_dict=True)
        ec_jwk: dict[str, Any] = ECAlgorithm.to_jwk(self.ec_key.public_key(), as_dict=True)
        rsa_jwk.update({"kid": "rsa-1", "use": "sig", "alg": "RS256"})
        ec_jwk.update({"kid": "ec-1", "use": "sig", "alg": "ES256"})
        return {"keys": [rsa_jwk, ec_jwk]}

    def id_token(
        self,
        *,
        nonce: str | None = None,
        exp_in: float = 300,
        aud: str | None = None,
        iss: str | None = None,
        alg: str = "RS256",
        kid: str | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {
            **self.user,
            "iss": iss or self.issuer,
            "aud": aud or self.client_id,
            "iat": now - 5,
            "exp": int(now + exp_in),
            **(extra or {}),
        }
        if nonce is not None:
            claims["nonce"] = nonce
        if alg == "ES256":
            return jwt.encode(
                claims, self.ec_key, algorithm="ES256", headers={"kid": kid or "ec-1"}
            )
        if alg == "HS256":
            return jwt.encode(claims, "x" * 32, algorithm="HS256", headers={"kid": kid or "rsa-1"})
        return jwt.encode(claims, self.rsa_key, algorithm="RS256", headers={"kid": kid or "rsa-1"})

    # -- endpoints --

    async def _discovery(self, request: Request) -> Response:
        base = self.issuer.rstrip("/")
        doc: dict[str, Any] = {
            "issuer": self.advertised_issuer or self.issuer,
            "authorization_endpoint": f"{base}/authorize",
            "token_endpoint": f"{base}/token",
            "jwks_uri": f"{base}/jwks",
            "response_types_supported": ["code"],
            "code_challenge_methods_supported": ["S256"],
        }
        if self.revocation:
            doc["revocation_endpoint"] = f"{base}/revoke"
        return JSONResponse(doc)

    async def _jwks(self, request: Request) -> Response:
        return JSONResponse(self.jwk_set())

    async def _authorize(self, request: Request) -> Response:
        query = request.query_params
        if query.get("client_id") != self.client_id or query.get("code_challenge_method") != "S256":
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        code = secrets.token_urlsafe(24)
        self.codes[code] = _Code(
            challenge=query.get("code_challenge", ""),
            redirect_uri=query.get("redirect_uri", ""),
            nonce=query.get("nonce", ""),
            claims=dict(self.user),
        )
        target = query.get("redirect_uri", "")
        joiner = "&" if "?" in target else "?"
        back = urlencode({"code": code, "state": query.get("state", "")})
        return RedirectResponse(f"{target}{joiner}{back}", status_code=302)

    def _tokens(self, *, nonce: str | None, with_id_token: bool = True) -> dict[str, Any]:
        refresh = secrets.token_urlsafe(24)
        self.refresh_tokens[refresh] = dict(self.user)
        tokens: dict[str, Any] = {
            "access_token": secrets.token_urlsafe(16),
            "token_type": "Bearer",
            "expires_in": self.expires_in,
            "refresh_token": refresh,
        }
        if with_id_token:
            tokens["id_token"] = self.id_token(nonce=nonce)
        return tokens

    async def _token(self, request: Request) -> Response:
        form = await request.form()
        grant = form.get("grant_type")
        if form.get("client_id") != self.client_id:
            return JSONResponse({"error": "invalid_client"}, status_code=401)
        if grant == "authorization_code":
            with self._lock:
                self.grant_calls += 1
            found = self.codes.pop(str(form.get("code", "")), None)
            if found is None or found.redirect_uri != form.get("redirect_uri"):
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            if _s256(str(form.get("code_verifier", ""))) != found.challenge:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            return JSONResponse(self._tokens(nonce=found.nonce))
        if grant == "refresh_token":
            with self._lock:
                self.refresh_calls += 1
            if self.refresh_gate is not None:
                self.refresh_gate.wait(5)
            token = str(form.get("refresh_token", ""))
            if self.refresh_tokens.pop(token, None) is None:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            return JSONResponse(self._tokens(nonce=None, with_id_token=self.refresh_on_id_token))
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    async def _revoke(self, request: Request) -> Response:
        form = await request.form()
        self.revoked.append(str(form.get("token", "")))
        return Response(status_code=200)
