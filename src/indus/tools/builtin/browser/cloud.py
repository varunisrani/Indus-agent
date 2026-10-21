"""Cloud browser providers (P24-38): Browser Use, Browserbase and Firecrawl over their REST APIs.

A profile picks one with ``browser.provider``. ``CloudBrowsers.open(name, scope, task_id)``
creates a remote browser session and returns its CDP address, which the session manager attaches
to like a user CDP endpoint (the request guard goes on our tab). ``release(scope, session)`` gives
the remote session back; it never raises, so it can run on every way out (close, idle reap, force
reap, shutdown, a failed attach).

Keys come from the profile's ``SecretStore`` first, then the scope's env, never the process env.
Vendor API versions (interop facts, see ``docs/notes/P24/browser-cloud.md``):

- Browser Use v3: ``POST {base}/browsers`` with ``X-Browser-Use-API-Key``; answer ``id`` and
  ``cdpUrl`` (or ``connectUrl``); release ``PATCH {base}/browsers/{id}`` ``{"action": "stop"}``.
- Browserbase v1: ``POST {base}/v1/sessions`` with ``X-BB-API-Key`` and ``projectId``; answer ``id``
  and ``connectUrl``; a 402 means a paid feature: ``keepAlive`` is dropped, then ``proxies``;
  release ``POST {base}/v1/sessions/{id}`` ``{"projectId", "status": "REQUEST_RELEASE"}``.
- Firecrawl v2: ``POST {base}/v2/browser`` with a bearer key and ``{"ttl": 300}``; answer ``id`` and
  ``cdpUrl``; release ``DELETE {base}/v2/browser/{id}``.

Errors (``CloudFailure``) name the provider, the HTTP status and at most 200 redacted characters
of the answer; they never carry a key. A missing key is ``CloudFailure`` too, naming the secret.
"""

from __future__ import annotations

import logging
import secrets as pysecrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, cast

import httpx

from indus.foundation.scope import Scope
from indus.safety.redaction import redact_output
from indus.safety.url_policy import proxy_for

__all__ = [
    "PROVIDERS",
    "PROVIDER_NAMES",
    "CloudBrowsers",
    "CloudFailure",
    "CloudProvider",
    "CloudSession",
    "credential",
]

log = logging.getLogger(__name__)

CREATE_TIMEOUT_S = 30.0
RELEASE_TIMEOUT_S = 10.0
RELEASED_STATUSES = frozenset({200, 201, 204})
BODY_SHOWN = 200
FIRECRAWL_TTL_S = 300


class CloudFailure(RuntimeError):
    """The provider could not give us a browser (the text is safe to show)."""

    def __init__(self, provider: str, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.provider = provider
        self.status = status


@dataclass(frozen=True)
class CloudSession:
    """One remote browser: the provider's id, where to attach, and what it was opened with."""

    provider: str
    session_id: str
    cdp_url: str = field(repr=False)
    name: str
    expires_at: str | None = None
    features: Mapping[str, bool] = field(default_factory=dict[str, bool])


def credential(scope: Scope, name: str) -> str | None:
    """``name`` from the profile's secrets, then the scope's env; ``None`` when unset."""
    if scope.secrets is not None:
        try:
            value = scope.secrets.get(name)
        except Exception:
            log.debug("secret lookup of %s failed", name, exc_info=True)
            value = None
        if value:
            return value
    found = scope.env.get(name)
    return found or None


def _session_name(task_id: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in task_id)[:40] or "task"
    return f"indus-{safe}-{pysecrets.token_hex(4)}"


def _answer_text(response: httpx.Response, scope: Scope) -> str:
    try:
        text = response.text
    except Exception:
        return ""
    return redact_output(text[:BODY_SHOWN], force=True, scope=scope)


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {}
    return cast(dict[str, Any], data) if isinstance(data, dict) else {}


type ClientFactory = Callable[[Scope, float], httpx.AsyncClient]


def default_client(scope: Scope, timeout_s: float) -> httpx.AsyncClient:
    """An HTTP client for the provider API: the profile's proxy, never the process env."""
    return httpx.AsyncClient(
        timeout=timeout_s, trust_env=False, proxy=proxy_for(scope, "https"), follow_redirects=False
    )


class CloudProvider:
    """What every provider has: a name, a label, the secret names and the two calls."""

    name: str = ""
    label: str = ""
    secret_names: tuple[str, ...] = ()

    def config(self, scope: Scope) -> dict[str, str] | None:
        """The keys and base URL, or ``None`` when a key is missing."""
        raise NotImplementedError

    def missing(self) -> str:
        names = " and ".join(self.secret_names)
        return (
            f"{self.label} needs {names} in this profile's secrets "
            f"(indus config set {self.secret_names[0]})."
        )

    def ready(self, scope: Scope) -> bool:
        return self.config(scope) is not None

    def _config_or_fail(self, scope: Scope) -> dict[str, str]:
        found = self.config(scope)
        if found is None:
            raise CloudFailure(self.name, self.missing())
        return found

    async def _post(
        self, client: httpx.AsyncClient, url: str, headers: dict[str, str], body: dict[str, Any]
    ) -> httpx.Response:
        try:
            return await client.post(url, headers=headers, json=body)
        except httpx.HTTPError as exc:
            raise CloudFailure(
                self.name, f"{self.label} could not be reached ({type(exc).__name__})."
            ) from None

    def _created(self, response: httpx.Response, scope: Scope) -> dict[str, Any]:
        if not response.is_success:
            raise CloudFailure(
                self.name,
                f"{self.label} refused the browser session: HTTP {response.status_code} "
                f"{_answer_text(response, scope)}".rstrip(),
                status=response.status_code,
            )
        data = _json(response)
        if not isinstance(data.get("id"), str) or not data.get("id"):
            raise CloudFailure(self.name, f"{self.label} answered without a session id.")
        return data

    async def create(self, client: httpx.AsyncClient, scope: Scope, task_id: str) -> CloudSession:
        raise NotImplementedError

    async def release(self, client: httpx.AsyncClient, scope: Scope, session_id: str) -> bool:
        raise NotImplementedError

    def _released(self, response: httpx.Response, session_id: str) -> bool:
        if response.status_code in RELEASED_STATUSES:
            log.debug("%s session %s released", self.label, session_id)
            return True
        log.warning(
            "%s did not release session %s: HTTP %s", self.label, session_id, response.status_code
        )
        return False


class BrowserUse(CloudProvider):
    name = "browser-use"
    label = "Browser Use"
    secret_names = ("BROWSER_USE_API_KEY",)
    base_url = "https://api.browser-use.com/api/v3"

    def config(self, scope: Scope) -> dict[str, str] | None:
        key = credential(scope, "BROWSER_USE_API_KEY")
        return {"key": key, "base": self.base_url} if key else None

    @staticmethod
    def _headers(conf: Mapping[str, str]) -> dict[str, str]:
        return {"Content-Type": "application/json", "X-Browser-Use-API-Key": conf["key"]}

    async def create(self, client: httpx.AsyncClient, scope: Scope, task_id: str) -> CloudSession:
        conf = self._config_or_fail(scope)
        response = await self._post(client, f"{conf['base']}/browsers", self._headers(conf), {})
        data = self._created(response, scope)
        cdp = data.get("cdpUrl") or data.get("connectUrl") or ""
        if not isinstance(cdp, str) or not cdp:
            raise CloudFailure(self.name, f"{self.label} answered without a CDP address.")
        expires = data.get("timeoutAt")
        return CloudSession(
            self.name,
            str(data["id"]),
            cdp,
            _session_name(task_id),
            expires if isinstance(expires, str) else None,
            {"browser_use": True},
        )

    async def release(self, client: httpx.AsyncClient, scope: Scope, session_id: str) -> bool:
        conf = self.config(scope)
        if conf is None:
            log.warning("cannot release %s session %s: no key", self.label, session_id)
            return False
        response = await client.patch(
            f"{conf['base']}/browsers/{session_id}",
            headers=self._headers(conf),
            json={"action": "stop"},
        )
        return self._released(response, session_id)


class Browserbase(CloudProvider):
    name = "browserbase"
    label = "Browserbase"
    secret_names = ("BROWSERBASE_API_KEY", "BROWSERBASE_PROJECT_ID")
    base_url = "https://api.browserbase.com"
    # Dropped one at a time, in this order, when the plan does not include them (HTTP 402).
    paid_features = ("keepAlive", "proxies")

    def config(self, scope: Scope) -> dict[str, str] | None:
        key = credential(scope, "BROWSERBASE_API_KEY")
        project = credential(scope, "BROWSERBASE_PROJECT_ID")
        if not (key and project):
            return None
        base = (credential(scope, "BROWSERBASE_BASE_URL") or self.base_url).rstrip("/")
        return {"key": key, "project": project, "base": base}

    @staticmethod
    def _headers(conf: Mapping[str, str]) -> dict[str, str]:
        return {"Content-Type": "application/json", "X-BB-API-Key": conf["key"]}

    async def create(self, client: httpx.AsyncClient, scope: Scope, task_id: str) -> CloudSession:
        conf = self._config_or_fail(scope)
        body: dict[str, Any] = {"projectId": conf["project"], "keepAlive": True, "proxies": True}
        url = f"{conf['base']}/v1/sessions"
        response = await self._post(client, url, self._headers(conf), body)
        for feature in self.paid_features:
            if response.status_code != 402:
                break
            log.warning("%s: %s needs a paid plan (402); retrying without it", self.label, feature)
            body.pop(feature, None)
            response = await self._post(client, url, self._headers(conf), body)
        data = self._created(response, scope)
        cdp = data.get("connectUrl")
        if not isinstance(cdp, str) or not cdp:
            raise CloudFailure(self.name, f"{self.label} answered without a connect address.")
        features = {"keep_alive": "keepAlive" in body, "proxies": "proxies" in body}
        return CloudSession(self.name, str(data["id"]), cdp, _session_name(task_id), None, features)

    async def release(self, client: httpx.AsyncClient, scope: Scope, session_id: str) -> bool:
        conf = self.config(scope)
        if conf is None:
            log.warning("cannot release %s session %s: no key", self.label, session_id)
            return False
        response = await client.post(
            f"{conf['base']}/v1/sessions/{session_id}",
            headers=self._headers(conf),
            json={"projectId": conf["project"], "status": "REQUEST_RELEASE"},
        )
        return self._released(response, session_id)


class Firecrawl(CloudProvider):
    name = "firecrawl"
    label = "Firecrawl"
    secret_names = ("FIRECRAWL_API_KEY",)
    base_url = "https://api.firecrawl.dev"

    def config(self, scope: Scope) -> dict[str, str] | None:
        key = credential(scope, "FIRECRAWL_API_KEY")
        if not key:
            return None
        base = (credential(scope, "FIRECRAWL_API_URL") or self.base_url).rstrip("/")
        return {"key": key, "base": base}

    @staticmethod
    def _headers(conf: Mapping[str, str]) -> dict[str, str]:
        return {"Content-Type": "application/json", "Authorization": f"Bearer {conf['key']}"}

    async def create(self, client: httpx.AsyncClient, scope: Scope, task_id: str) -> CloudSession:
        conf = self._config_or_fail(scope)
        response = await self._post(
            client, f"{conf['base']}/v2/browser", self._headers(conf), {"ttl": FIRECRAWL_TTL_S}
        )
        data = self._created(response, scope)
        cdp = data.get("cdpUrl")
        if not isinstance(cdp, str) or not cdp:
            raise CloudFailure(self.name, f"{self.label} answered without a CDP address.")
        return CloudSession(
            self.name, str(data["id"]), cdp, _session_name(task_id), None, {"firecrawl": True}
        )

    async def release(self, client: httpx.AsyncClient, scope: Scope, session_id: str) -> bool:
        conf = self.config(scope)
        if conf is None:
            log.warning("cannot release %s session %s: no key", self.label, session_id)
            return False
        response = await client.delete(
            f"{conf['base']}/v2/browser/{session_id}", headers=self._headers(conf)
        )
        return self._released(response, session_id)


PROVIDERS: dict[str, CloudProvider] = {
    p.name: p for p in (BrowserUse(), Browserbase(), Firecrawl())
}
PROVIDER_NAMES: frozenset[str] = frozenset(PROVIDERS)


class CloudBrowsers:
    """Creates and releases remote browser sessions (see the module notes)."""

    def __init__(self, client_factory: ClientFactory = default_client) -> None:
        self.client_factory = client_factory

    def provider(self, name: str) -> CloudProvider:
        found = PROVIDERS.get(name)
        if found is None:
            raise CloudFailure(name, f"Unknown browser provider {name!r}.")
        return found

    async def open(self, name: str, scope: Scope, task_id: str) -> CloudSession:
        provider = self.provider(name)
        async with self.client_factory(scope, CREATE_TIMEOUT_S) as client:
            made = await provider.create(client, scope, task_id)
        log.info("%s browser session %s created for %s", provider.label, made.name, task_id)
        return made

    async def release(self, scope: Scope, session: CloudSession) -> bool:
        """Give the remote browser back; logs and returns False on any failure."""
        provider = PROVIDERS.get(session.provider)
        if provider is None:
            return False
        try:
            async with self.client_factory(scope, RELEASE_TIMEOUT_S) as client:
                return await provider.release(client, scope, session.session_id)
        except Exception as exc:
            log.warning(
                "releasing %s session %s failed: %s",
                provider.label,
                session.session_id,
                type(exc).__name__,
            )
            return False
