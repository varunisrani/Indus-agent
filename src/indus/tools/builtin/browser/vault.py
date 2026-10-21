"""``browser_vault`` (P24-40): model-blind autofill from 1Password, Bitwarden or a local vault.

The model works with handles and never sees a secret. Actions:

- ``list``: every item's handle, backend, kind (``login``, ``payment``, ``address``), label, bound
  origin(s) and, for logins, the identifier (not a secret: the agent types it itself); managers
  that are locked are listed under ``locked``.
- ``fill {handle}``: resolves the secret here and writes it into the current page only when the
  page's origin is exactly one of the item's origins: checked before, and again inside the fill
  script, so a page that moved in between gets nothing (``origin_changed``). Logins fill the
  password field, payment items the card fields after the user approves an approval card,
  address items the address fields.
- ``save_login {label?}``: asks the user for the current page's identifier (a clarify question)
  and password (a hidden secret request), stores them in the local vault bound to that origin,
  then fills the password.
- ``enter_code {handle?}``: fills a one-time code, from the item's TOTP seed when it has one
  (computed by the manager, or by ``cryptography``'s TOTP for the local vault), else asked of
  the user.
- ``unlock {backend}``: asks the user for a manager's master password; for the local vault (when
  ``indus vault lock`` locked it) asks the user's approval to open it for this process.

Results carry handles, counts, origins and outcomes only. Every value written into a page is
added to the session's redaction set (its scope's secrets view), and the filled fields are marked
so outlines show ``[hidden]`` instead of their value. Exception texts from a page evaluation are
never shown (they can quote the script); messages are fixed and scrubbed of every secret value.

Backends: the local vault (``<home>/vault/local.vault``, a Fernet token written 0600; its key is
``INDUS_VAULT_KEY`` in the profile's ``SecretStore``, the OS keychain when it works),
1Password's ``op`` CLI v2 and Bitwarden's ``bw`` CLI. Master passwords reach ``op`` on stdin and
``bw`` through an environment variable of its own child; session tokens stay in this process's
memory, per profile, and travel to the CLIs by environment, never argv. ``browser.vault_sources``
lists the backends to use; a CLI backend is used only when its program is installed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets as pysecrets
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

from indus.foundation.config.schema import default_for
from indus.foundation.fsio import atomic_write_bytes
from indus.foundation.scope import Scope, SecretSource, scope_or_none
from indus.protocol.requests_port import RequestNotAnswered, ServerRequestPort
from indus.protocol.server_requests import (
    ApprovalAnswer,
    ApprovalChoice,
    ClarifyAnswer,
    SecretAnswer,
)
from indus.safety.child_env import child_env
from indus.tools.builtin.browser.engines import locate_tool
from indus.tools.builtin.browser.session import BrowserSession, SessionManager
from indus.tools.builtin.browser.tools import as_json, fail
from indus.tools.registry import ToolRegistry, ToolTraits, scope_key

__all__ = [
    "ADDRESS_FIELDS",
    "KINDS",
    "PAYMENT_FIELDS",
    "SCHEMA",
    "AsyncCliRunner",
    "Bitwarden",
    "ItemMeta",
    "LocalVault",
    "OnePassword",
    "VaultError",
    "VaultTool",
    "normalize_origin",
    "register_vault_tool",
]

log = logging.getLogger(__name__)

TOOL_NAME = "browser_vault"
TOOLSET = "browser"
KINDS = ("login", "payment", "address")
PAYMENT_FIELDS = ("cc_name", "cc_number", "cc_exp", "cc_csc")
ADDRESS_FIELDS = (
    "name",
    "street",
    "city",
    "region",
    "postal_code",
    "country",
    "phone",
    "email",
)
REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "login": ("password",),
    "payment": ("cc_number", "cc_exp", "cc_csc"),
    "address": ("street", "city", "postal_code"),
}
VAULT_DIR = Path("vault")
VAULT_FILE = "local.vault"
LOCK_MARKER = "locked"
KEY_NAME = "INDUS_VAULT_KEY"
CLI_TIMEOUT_S = 30.0
PROMPT_TIMEOUT_S = 300.0
STAMP_ATTR = "data-indus-vault"
FILLED_ATTR = "data-indus-filled"


class VaultError(RuntimeError):
    """A vault operation failed; the text is safe to show."""


class UnlockRequired(VaultError):
    pass


def normalize_origin(text: str) -> str | None:
    """``scheme://host[:port]`` of an http(s) URL or origin (default ports dropped), else None."""
    try:
        parts = urlsplit(text.strip())
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower().rstrip(".")
    if scheme not in ("http", "https") or not host:
        return None
    if ":" in host:
        host = f"[{host}]"
    default = 443 if scheme == "https" else 80
    return f"{scheme}://{host}" + (f":{port}" if port and port != default else "")


@dataclass(frozen=True)
class ItemMeta:
    """What may be shown about a vault item (never its secret)."""

    handle: str
    backend: str
    kind: str
    label: str
    origins: tuple[str, ...] = ()
    identifier: str = ""
    identifier_type: str = ""
    has_otp: bool = False

    @property
    def origin(self) -> str:
        return self.origins[0] if self.origins else ""

    def public(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "handle": self.handle,
            "backend": self.backend,
            "kind": self.kind,
            "label": self.label,
            "origin": self.origin,
        }
        if len(self.origins) > 1:
            row["allowed_origins"] = list(self.origins)
        if self.identifier:
            row["identifier"] = self.identifier
            row["identifier_type"] = self.identifier_type or "username"
        if self.has_otp:
            row["two_factor"] = "automatic"
        return row


def identifier_type(identifier: str) -> str:
    if "@" in identifier:
        return "email"
    return "phone" if identifier.lstrip("+").replace(" ", "").isdigit() else "username"


# -- backends -----------------------------------------------------------------------------------


class Backend(Protocol):
    name: str
    display_name: str
    needs_unlock: bool

    def owns(self, handle: str) -> bool: ...

    def available(self, scope: Scope) -> bool: ...

    def unlocked(self, scope: Scope) -> bool: ...

    async def unlock(self, scope: Scope, master: str) -> None: ...

    async def items(self, scope: Scope) -> list[ItemMeta]: ...

    async def secret(self, scope: Scope, handle: str) -> dict[str, str]: ...

    async def otp(self, scope: Scope, handle: str) -> str | None: ...


def _fernet(key: bytes) -> Any:
    try:
        from cryptography.fernet import Fernet  # the `vault` extra (already locked)
    except ImportError as exc:  # pragma: no cover - cryptography is installed with keyring
        raise VaultError(
            "The local vault needs the 'vault' extra (cryptography). Install it with: "
            "uv pip install 'indus-agent[vault]'"
        ) from exc
    return Fernet(key)


class LocalVault:
    """The encrypted vault in the profile home (see the module notes)."""

    name = "local"
    display_name = "Local vault"
    needs_unlock = True  # only while `indus vault lock` holds it

    def __init__(self) -> None:
        self._opened: set[str] = set()  # profiles unlocked for this process despite the marker

    @staticmethod
    def folder(scope: Scope) -> Path:
        return scope.home / VAULT_DIR

    def owns(self, handle: str) -> bool:
        return handle.startswith("local-")

    def available(self, scope: Scope) -> bool:
        return True

    def locked(self, scope: Scope) -> bool:
        return (self.folder(scope) / LOCK_MARKER).exists()

    def unlocked(self, scope: Scope) -> bool:
        return not self.locked(scope) or scope_key(scope) in self._opened

    async def unlock(self, scope: Scope, master: str) -> None:
        self._opened.add(scope_key(scope))

    def lock(self, scope: Scope) -> None:
        folder = self.folder(scope)
        folder.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(folder / LOCK_MARKER, b"locked\n", mode=0o600)
        self._opened.discard(scope_key(scope))

    def unlock_marker(self, scope: Scope) -> bool:
        marker = self.folder(scope) / LOCK_MARKER
        existed = marker.exists()
        marker.unlink(missing_ok=True)
        return existed

    @staticmethod
    def _key(scope: Scope, *, create: bool) -> bytes | None:
        store = scope.secrets
        value: str | None = None
        if store is not None:
            with contextlib.suppress(Exception):
                value = store.get(KEY_NAME)
        if value:
            return value.encode("ascii")
        if not create:
            return None
        setter = getattr(store, "set", None)
        if not callable(setter):
            raise VaultError(
                "The local vault has no key yet and this session cannot store one; run "
                "`indus vault add` once in a terminal."
            )
        from cryptography.fernet import Fernet

        made = Fernet.generate_key()
        setter(KEY_NAME, made.decode("ascii"))
        return made

    def _load(self, scope: Scope) -> list[dict[str, Any]]:
        path = self.folder(scope) / VAULT_FILE
        if not path.exists():
            return []
        key = self._key(scope, create=False)
        if key is None:
            raise VaultError("The local vault's key is missing from this profile's secrets.")
        try:
            from cryptography.fernet import InvalidToken

            raw = _fernet(key).decrypt(path.read_bytes())
        except InvalidToken:
            raise VaultError(
                "The local vault could not be opened with this profile's key."
            ) from None
        data = json.loads(raw)
        return cast(list[dict[str, Any]], data) if isinstance(data, list) else []

    def _save(self, scope: Scope, items: list[dict[str, Any]]) -> None:
        key = self._key(scope, create=True)
        assert key is not None
        folder = self.folder(scope)
        folder.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            folder.chmod(0o700)
        token = _fernet(key).encrypt(json.dumps(items).encode("utf-8"))
        atomic_write_bytes(folder / VAULT_FILE, token, mode=0o600)

    @staticmethod
    def _meta(row: Mapping[str, Any]) -> ItemMeta:
        origins = tuple(str(o) for o in cast(list[Any], row.get("origins") or []))
        return ItemMeta(
            handle=str(row["id"]),
            backend="local",
            kind=str(row.get("kind") or "login"),
            label=str(row.get("label") or ""),
            origins=origins,
            identifier=str(row.get("identifier") or ""),
            identifier_type=str(row.get("identifier_type") or ""),
            has_otp=bool(row.get("otp_seed")),
        )

    def add(
        self,
        scope: Scope,
        *,
        kind: str,
        label: str,
        origin: str,
        secret: Mapping[str, str],
        identifier: str = "",
        otp_seed: str = "",
    ) -> ItemMeta:
        if kind not in KINDS:
            raise VaultError(f"kind must be one of {', '.join(KINDS)}")
        bound = normalize_origin(origin)
        if bound is None:
            raise VaultError("the origin must be an http(s) address such as https://example.com")
        missing = [f for f in REQUIRED_FIELDS[kind] if not secret.get(f)]
        if missing:
            raise VaultError(f"missing {', '.join(missing)}")
        allowed = PAYMENT_FIELDS if kind == "payment" else ADDRESS_FIELDS
        kept = {k: v for k, v in secret.items() if v and (kind == "login" or k in allowed)}
        if kind == "login":
            kept = {"password": secret["password"]}
        items = self._load(scope)
        row: dict[str, Any] = {
            "id": f"local-{pysecrets.token_hex(6)}",
            "kind": kind,
            "label": label.strip() or bound.split("://", 1)[1],
            "origins": [bound],
            "secret": kept,
        }
        if kind == "login":
            row["identifier"] = identifier.strip()
            row["identifier_type"] = identifier_type(identifier.strip())
        if otp_seed.strip():
            row["otp_seed"] = otp_seed.strip().replace(" ", "").upper()
        items.append(row)
        self._save(scope, items)
        return self._meta(row)

    def remove(self, scope: Scope, handle: str) -> bool:
        items = self._load(scope)
        kept = [row for row in items if row.get("id") != handle]
        if len(kept) == len(items):
            return False
        self._save(scope, kept)
        return True

    def list_items(self, scope: Scope) -> list[ItemMeta]:
        return [self._meta(row) for row in self._load(scope)]

    async def items(self, scope: Scope) -> list[ItemMeta]:
        return self.list_items(scope)

    def _row(self, scope: Scope, handle: str) -> dict[str, Any]:
        for row in self._load(scope):
            if row.get("id") == handle:
                return row
        raise VaultError(f"No vault item with handle {handle!r}.")

    async def secret(self, scope: Scope, handle: str) -> dict[str, str]:
        stored = self._row(scope, handle).get("secret")
        return {str(k): str(v) for k, v in cast(dict[str, Any], stored or {}).items()}

    async def otp(self, scope: Scope, handle: str) -> str | None:
        seed = self._row(scope, handle).get("otp_seed")
        if not isinstance(seed, str) or not seed:
            return None
        return totp_now(seed, scope.clock.now().timestamp())


def totp_now(seed: str, at: float) -> str:
    """The current 6-digit, 30 s TOTP code of a base32 seed (``cryptography``'s TOTP)."""
    import base64

    from cryptography.hazmat.primitives.hashes import SHA1
    from cryptography.hazmat.primitives.twofactor.totp import TOTP

    padded = seed + "=" * (-len(seed) % 8)
    key = base64.b32decode(padded, casefold=True)
    code = TOTP(key, 6, SHA1(), 30, enforce_key_length=False).generate(int(at))
    return code.decode("ascii")


# -- password-manager CLIs -------------------------------------------------------------------


@dataclass(frozen=True)
class CliResult:
    returncode: int
    stdout: str
    stderr: str


class CliRunner(Protocol):
    async def run(
        self,
        argv: Sequence[str],
        env: Mapping[str, str],
        *,
        stdin: str | None = None,
        timeout_s: float = CLI_TIMEOUT_S,
    ) -> CliResult: ...


class AsyncCliRunner:
    async def run(
        self,
        argv: Sequence[str],
        env: Mapping[str, str],
        *,
        stdin: str | None = None,
        timeout_s: float = CLI_TIMEOUT_S,
    ) -> CliResult:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=dict(env),
        )
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(stdin.encode("utf-8") if stdin is not None else None), timeout_s
            )
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
            raise VaultError(
                f"{Path(argv[0]).name} did not answer within {timeout_s:g} s"
            ) from None
        return CliResult(
            proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
        )


def _origins(urls: Iterable[object]) -> tuple[str, ...]:
    found: list[str] = []
    for url in urls:
        origin = normalize_origin(url) if isinstance(url, str) else None
        if origin and origin not in found:
            found.append(origin)
    return tuple(found)


class _CliBackend:
    """Shared by the two manager CLIs: program lookup, session tokens, running commands."""

    name = ""
    display_name = ""
    program = ""
    prefix = ""
    token_env = ""
    needs_unlock = True

    def __init__(
        self,
        runner: CliRunner | None = None,
        locate: Callable[[Scope, str], Path | None] = locate_tool,
    ) -> None:
        self.runner: CliRunner = runner if runner is not None else AsyncCliRunner()
        self.locate = locate
        self._tokens: dict[str, str] = {}

    def owns(self, handle: str) -> bool:
        return handle.startswith(self.prefix)

    def available(self, scope: Scope) -> bool:
        return self.locate(scope, self.program) is not None

    def unlocked(self, scope: Scope) -> bool:
        return scope_key(scope) in self._tokens

    def lock(self, scope: Scope) -> None:
        self._tokens.pop(scope_key(scope), None)

    def _binary(self, scope: Scope) -> str:
        found = self.locate(scope, self.program)
        if found is None:
            raise VaultError(f"{self.display_name}'s `{self.program}` program is not installed.")
        return str(found)

    def _env(self, scope: Scope, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        env = child_env(scope, variant="code")
        token = self._tokens.get(scope_key(scope))
        if token:
            env[self.token_env] = token
        env.update(extra or {})
        return env

    def _store_token(self, scope: Scope, result: CliResult) -> None:
        token = result.stdout.strip()
        if result.returncode != 0 or not token:
            raise VaultError(f"{self.display_name} did not unlock (wrong password?).")
        self._tokens[scope_key(scope)] = token

    async def _run(self, scope: Scope, *args: str) -> str:
        if not self.unlocked(scope):
            raise UnlockRequired(f"{self.display_name} is locked.")
        result = await self.runner.run([self._binary(scope), *args], self._env(scope))
        if result.returncode != 0:
            if "lock" in result.stderr.lower() or "sign" in result.stderr.lower():
                self.lock(scope)
                raise UnlockRequired(f"{self.display_name} locked again.")
            raise VaultError(f"{self.display_name} answered with exit code {result.returncode}.")
        return result.stdout

    def _id(self, handle: str) -> str:
        if not self.owns(handle):
            raise VaultError(f"{handle!r} is not a {self.display_name} handle.")
        return handle[len(self.prefix) :]


class OnePassword(_CliBackend):
    """1Password CLI v2 (``op``)."""

    name = "onepassword"
    display_name = "1Password"
    program = "op"
    prefix = "op-"
    token_env = "OP_SESSION"

    async def unlock(self, scope: Scope, master: str) -> None:
        result = await self.runner.run(
            [self._binary(scope), "signin", "--raw"], self._env(scope), stdin=master
        )
        self._store_token(scope, result)

    @staticmethod
    def _split(item_id: str) -> list[str]:
        vault, _, item = item_id.partition(":")
        return [item, "--vault", vault] if item else [vault]

    async def items(self, scope: Scope) -> list[ItemMeta]:
        raw = await self._run(scope, "item", "list", "--categories", "Login", "--format", "json")
        try:
            rows = json.loads(raw or "[]")
        except ValueError:
            raise VaultError("1Password answered with unreadable JSON.") from None
        found: list[ItemMeta] = []
        for row in cast(list[Any], rows if isinstance(rows, list) else []):
            if not isinstance(row, dict):
                continue
            item = cast(dict[str, Any], row)
            vault = cast(dict[str, Any], item.get("vault") or {}).get("id") or ""
            urls = [
                cast(dict[str, Any], u).get("href")
                for u in cast(list[Any], item.get("urls") or [])
                if isinstance(u, dict)
            ]
            ident = str(item.get("additional_information") or "")
            found.append(
                ItemMeta(
                    handle=f"{self.prefix}{vault}:{item.get('id')}"
                    if vault
                    else f"{self.prefix}{item.get('id')}",
                    backend=self.name,
                    kind="login",
                    label=str(item.get("title") or ""),
                    origins=_origins(urls),
                    identifier=ident,
                    identifier_type=identifier_type(ident) if ident else "",
                )
            )
        return found

    async def secret(self, scope: Scope, handle: str) -> dict[str, str]:
        out = await self._run(
            scope,
            "item",
            "get",
            *self._split(self._id(handle)),
            "--fields",
            "label=password",
            "--reveal",
        )
        return {"password": out.rstrip("\r\n")}

    async def otp(self, scope: Scope, handle: str) -> str | None:
        with contextlib.suppress(VaultError):
            code = await self._run(scope, "item", "get", *self._split(self._id(handle)), "--otp")
            return code.strip() or None
        return None


class Bitwarden(_CliBackend):
    """Bitwarden CLI (``bw``)."""

    name = "bitwarden"
    display_name = "Bitwarden"
    program = "bw"
    prefix = "bw-"
    token_env = "BW_SESSION"
    master_env = "INDUS_BW_MASTER"

    async def unlock(self, scope: Scope, master: str) -> None:
        argv = [
            self._binary(scope),
            "unlock",
            "--raw",
            "--nointeraction",
            "--passwordenv",
            self.master_env,
        ]
        result = await self.runner.run(argv, self._env(scope, {self.master_env: master}))
        self._store_token(scope, result)

    async def items(self, scope: Scope) -> list[ItemMeta]:
        raw = await self._run(scope, "list", "items", "--nointeraction")
        try:
            rows = json.loads(raw or "[]")
        except ValueError:
            raise VaultError("Bitwarden answered with unreadable JSON.") from None
        found: list[ItemMeta] = []
        for row in cast(list[Any], rows if isinstance(rows, list) else []):
            if not isinstance(row, dict) or cast(dict[str, Any], row).get("type") != 1:
                continue  # type 1 is a login
            item = cast(dict[str, Any], row)
            login = cast(dict[str, Any], item.get("login") or {})
            uris = [
                cast(dict[str, Any], u).get("uri")
                for u in cast(list[Any], login.get("uris") or [])
                if isinstance(u, dict)
            ]
            ident = str(login.get("username") or "")
            found.append(
                ItemMeta(
                    handle=f"{self.prefix}{item.get('id')}",
                    backend=self.name,
                    kind="login",
                    label=str(item.get("name") or ""),
                    origins=_origins(uris),
                    identifier=ident,
                    identifier_type=identifier_type(ident) if ident else "",
                    has_otp=bool(login.get("totp")),
                )
            )
        return found

    async def secret(self, scope: Scope, handle: str) -> dict[str, str]:
        out = await self._run(scope, "get", "password", self._id(handle), "--nointeraction")
        return {"password": out.rstrip("\r\n")}

    async def otp(self, scope: Scope, handle: str) -> str | None:
        with contextlib.suppress(VaultError):
            code = await self._run(scope, "get", "totp", self._id(handle), "--nointeraction")
            return code.strip() or None
        return None


# -- the page side ----------------------------------------------------------------------------

INSPECT_JS = """(nonce) => {
  const out = [];
  let i = 0;
  for (const el of document.querySelectorAll('input, select, textarea')) {
    const box = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    if (box.width <= 0 || box.height <= 0 || style.visibility === 'hidden') continue;
    if (el.disabled || el.readOnly) continue;
    const t = (el.getAttribute('type') || 'text').toLowerCase();
    if (['hidden', 'submit', 'button', 'checkbox', 'radio', 'image', 'reset'].includes(t)) continue;
    const stamp = nonce + '-' + (i++);
    el.setAttribute('ATTR', stamp);
    const label = (el.labels && el.labels.length ? el.labels[0].innerText : '') ||
      el.getAttribute('aria-label') || '';
    out.push({stamp: stamp, type: t, name: el.name || '', id: el.id || '',
      autocomplete: (el.getAttribute('autocomplete') || '').toLowerCase(),
      placeholder: el.getAttribute('placeholder') || '', label: label.slice(0, 80),
      maxlength: el.maxLength > 0 ? el.maxLength : 0});
  }
  return out;
}""".replace("ATTR", STAMP_ATTR)

FILL_JS = """(args) => {
  if (location.origin !== args.expected) return {refused: 'origin_changed', found: location.origin};
  let filled = 0;
  for (const pair of args.fills) {
    const el = document.querySelector('[STAMP="' + pair[0] + '"]');
    if (!el) continue;
    const proto = el.tagName === 'SELECT' ? HTMLSelectElement.prototype :
      el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
    el.focus();
    setter.call(el, pair[1]);
    el.setAttribute('FILLED', '1');
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
    filled += 1;
  }
  for (const el of document.querySelectorAll('[STAMP]')) el.removeAttribute('STAMP');
  return {filled: filled};
}""".replace("STAMP", STAMP_ATTR).replace("FILLED", FILLED_ATTR)


def inspect_expression(nonce: str) -> str:
    return f"({INSPECT_JS})({json.dumps(nonce)})"


def fill_expression(expected: str, fills: Sequence[tuple[str, str]]) -> str:
    args = {"expected": expected, "fills": [list(pair) for pair in fills]}
    return f"({FILL_JS})({json.dumps(args)})"


def _hints(control: Mapping[str, Any]) -> str:
    return " ".join(
        str(control.get(k) or "").lower() for k in ("name", "id", "placeholder", "label")
    )


_PAYMENT_TOKENS = {
    "cc-name": "cc_name",
    "cc-number": "cc_number",
    "cc-exp": "cc_exp",
    "cc-csc": "cc_csc",
}
_PAYMENT_HINTS = (
    ("cc_csc", ("cvc", "cvv", "csc", "security code")),
    ("cc_number", ("card number", "cardnumber", "card-number", "ccnum", "cc-number")),
    ("cc_exp", ("expiry", "expiration", "exp-date", "mm/yy", "cc-exp")),
    ("cc_name", ("name on card", "cardholder", "card holder", "cc-name")),
)
_ADDRESS_TOKENS = {
    "name": "name",
    "street-address": "street",
    "address-line1": "street",
    "address-level2": "city",
    "address-level1": "region",
    "postal-code": "postal_code",
    "country": "country",
    "country-name": "country",
    "tel": "phone",
    "email": "email",
}
_ADDRESS_HINTS = (
    ("postal_code", ("zip", "postal", "postcode")),
    ("street", ("street", "address")),
    ("city", ("city", "town")),
    ("region", ("state", "region", "province")),
    ("country", ("country",)),
    ("phone", ("phone", "tel")),
    ("email", ("email", "e-mail")),
)


def classify(kind: str, control: Mapping[str, Any]) -> str | None:
    """Which field of a ``kind`` item a page control takes, or None."""
    ac = str(control.get("autocomplete") or "")
    kind_of = str(control.get("type") or "")
    hints = _hints(control)
    if kind == "login":
        if kind_of == "password" and ac != "new-password":
            return "password"
        return None
    if kind == "otp":
        if ac == "one-time-code":
            return "code"
        if kind_of in ("text", "tel", "number") and any(
            word in hints for word in ("otp", "code", "totp", "2fa", "mfa", "verification")
        ):
            return "code"
        return None
    if kind == "payment":
        token = _PAYMENT_TOKENS.get(ac.split()[-1] if ac else "")
        if token:
            return token
        for name, words in _PAYMENT_HINTS:
            if any(word in hints for word in words):
                return name
        return None
    if kind == "address":
        token = _ADDRESS_TOKENS.get(ac.split()[-1] if ac else "")
        if token:
            return token
        for name, words in _ADDRESS_HINTS:
            if any(word in hints for word in words):
                return name
        return None
    return None


class _Masked:
    """The session's secrets view plus the values the vault wrote into its pages."""

    def __init__(self, inner: SecretSource | None, values: Iterable[str]) -> None:
        self.inner = inner
        self.values = frozenset(v for v in values if v)

    def get(self, name: str, /) -> str | None:
        return self.inner.get(name) if self.inner is not None else None

    def known_values(self) -> list[str]:
        base = list(self.inner.known_values()) if self.inner is not None else []
        return [*base, *sorted(self.values)]

    @property
    def generation(self) -> int:
        inner = getattr(self.inner, "generation", 0)
        return (inner if isinstance(inner, int) else 0) * 1_000_003 + len(self.values)


def remember_values(session: BrowserSession, values: Iterable[str]) -> None:
    """Mask ``values`` in every later result of ``session`` (P24-40 redaction boundary)."""
    current = session.scope.secrets
    known = set(current.values) if isinstance(current, _Masked) else set()
    inner = current.inner if isinstance(current, _Masked) else current
    session.scope = session.scope.with_secrets(_Masked(inner, known | {v for v in values if v}))


def scrub(text: str, values: Iterable[str]) -> str:
    for value in sorted({v for v in values if v}, key=len, reverse=True):
        text = text.replace(value, "[redacted]")
    return text


# -- the tool -------------------------------------------------------------------------------

SCHEMA: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": (
        "Use saved logins, cards and addresses on the CURRENT browser page without the secret "
        "entering the conversation. Actions: list (call first: handles; logins show the "
        "identifier you type yourself; locked managers are listed); fill (handle: fills the "
        "password of a login, card fields after the user approves, address fields; refused "
        "unless the page origin matches the item exactly); save_login (nothing saved for this "
        "site: the user types it in a hidden prompt, it is saved and filled); enter_code (a "
        "one-time code after the password, generated or asked of the user); unlock (backend: "
        "ask the user to unlock 1Password, Bitwarden or the local vault). Never type, ask for or "
        "repeat a password, card number, CVC or code yourself. Stop when the user declines."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "fill", "save_login", "enter_code", "unlock"],
            },
            "handle": {"type": "string", "description": "fill / enter_code: a handle from list"},
            "backend": {
                "type": "string",
                "enum": ["local", "onepassword", "bitwarden"],
                "description": "unlock only",
            },
            "label": {"type": "string", "description": "save_login: a short site name"},
        },
        "required": ["action"],
    },
}


@dataclass
class _Page:
    session: BrowserSession
    origin: str


@dataclass
class VaultTool:
    """The ``browser_vault`` handler (see the module notes)."""

    manager: SessionManager
    local: LocalVault = field(default_factory=LocalVault)
    onepassword: OnePassword = field(default_factory=OnePassword)
    bitwarden: Bitwarden = field(default_factory=Bitwarden)

    def backends(self, scope: Scope) -> list[Backend]:
        wanted: object = None
        if scope.config is not None:
            with contextlib.suppress(Exception):
                wanted = scope.config.get("browser.vault_sources")
        names = wanted if isinstance(wanted, list) else default_for("browser.vault_sources")
        chosen: list[Backend] = []
        for backend in (self.local, self.onepassword, self.bitwarden):
            if backend.name in cast(list[Any], names) and backend.available(scope):
                chosen.append(backend)
        return chosen

    def ready(self, scope: Scope) -> bool:
        return True

    def _backend_for(self, scope: Scope, handle: str) -> Backend | None:
        return next((b for b in self.backends(scope) if b.owns(handle)), None)

    async def __call__(
        self,
        args: Mapping[str, Any],
        *,
        task_id: str | None = None,
        scope: Scope | None = None,
        broker: ServerRequestPort | None = None,
    ) -> str:
        resolved = scope if scope is not None else scope_or_none()
        if resolved is None:
            return fail("browser_vault needs a profile scope; none is active here.")
        action = str(args.get("action") or "").strip()
        handle = str(args.get("handle") or "").strip()
        try:
            if action == "list":
                return await self.list(resolved, broker)
            if action == "unlock":
                return await self.unlock(resolved, str(args.get("backend") or ""), broker)
            task = task_id or "default"
            if action == "fill":
                return await self.fill(resolved, task, handle, broker)
            if action == "save_login":
                return await self.save_login(resolved, task, str(args.get("label") or ""), broker)
            if action == "enter_code":
                return await self.enter_code(resolved, task, handle, broker)
        except VaultError as exc:
            return fail(str(exc))
        return fail(f"Unknown action {action!r}; use list, fill, save_login, enter_code or unlock.")

    # -- list / unlock ----------------------------------------------------------------------

    async def list(self, scope: Scope, broker: ServerRequestPort | None) -> str:
        items: list[dict[str, Any]] = []
        locked: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for backend in self.backends(scope):
            if backend.needs_unlock and not backend.unlocked(scope):
                locked.append(
                    {
                        "backend": backend.name,
                        "display_name": backend.display_name,
                        "unlock": "browser_vault action=unlock"
                        if broker is not None
                        else "unavailable_in_this_session",
                    }
                )
                continue
            try:
                metas = await backend.items(scope)
            except VaultError as exc:
                errors.append({"backend": backend.name, "error": str(exc)[:200]})
                continue
            items.extend(meta.public() for meta in metas)
        body: dict[str, Any] = {"success": True, "items": items}
        if not items:
            body["hint"] = (
                "No saved items. On a login page, call browser_vault action=save_login to ask the "
                "user to save one. Never type a password yourself or ask for one in chat."
            )
        if locked:
            body["locked"] = locked
        if errors:
            body["errors"] = errors
        return as_json(body)

    async def unlock(self, scope: Scope, name: str, broker: ServerRequestPort | None) -> str:
        backend = next((b for b in self.backends(scope) if b.name == name), None)
        if backend is None:
            return fail(f"No vault source named {name!r} is available.")
        if backend.unlocked(scope):
            return as_json({"success": True, "backend": name, "already_unlocked": True})
        if broker is None:
            return as_json(
                {
                    "success": False,
                    "error_type": "unlock_unavailable",
                    "error": f"{backend.display_name} is locked and this session cannot ask the "
                    "user. Unlock it from an interactive session first.",
                }
            )
        if isinstance(backend, LocalVault):
            if not await _approve(
                broker,
                "Open the local vault for this session",
                "The agent wants to use your saved logins (it never sees them).",
            ):
                return as_json(
                    {
                        "success": False,
                        "error_type": "unlock_cancelled",
                        "error": "The user did not unlock the local vault.",
                    }
                )
            await backend.unlock(scope, "")
            return as_json({"success": True, "backend": name})
        master = await _ask_secret(
            broker, "INDUS_VAULT_MASTER", f"Master password to unlock {backend.display_name}"
        )
        if not master:
            return as_json(
                {
                    "success": False,
                    "error_type": "unlock_cancelled",
                    "error": f"The user declined to unlock {backend.display_name}.",
                }
            )
        try:
            await backend.unlock(scope, master)
        except VaultError as exc:
            return as_json(
                {
                    "success": False,
                    "error_type": "unlock_failed",
                    "error": scrub(str(exc), [master]),
                }
            )
        finally:
            del master
        return as_json({"success": True, "backend": name})

    # -- page helpers -----------------------------------------------------------------------

    def _page(self, task_id: str) -> _Page | str:
        session = self.manager.current(task_id)
        if session is None:
            return fail("No browser page is open for this task; navigate to the page first.")
        origin = normalize_origin(session.page.url())
        if origin is None:
            return fail("The current page has no web origin; navigate to the site first.")
        return _Page(session, origin)

    async def _controls(self, page: _Page) -> tuple[str, list[dict[str, Any]]]:
        nonce = pysecrets.token_hex(8)
        try:
            raw = await self.manager.run(
                page.session, lambda p: p.evaluate(inspect_expression(nonce)), label="vault inspect"
            )
        except Exception as exc:
            raise VaultError(
                f"The page's fields could not be read ({type(exc).__name__})."
            ) from None
        if isinstance(raw, str):
            with contextlib.suppress(ValueError):
                raw = json.loads(raw)
        rows = (
            [cast(dict[str, Any], r) for r in cast(list[Any], raw) if isinstance(r, dict)]
            if isinstance(raw, list)
            else []
        )
        return nonce, rows

    async def _write(
        self, page: _Page, expected: str, fills: list[tuple[str, str]]
    ) -> dict[str, Any]:
        values = [value for _, value in fills]
        remember_values(page.session, values)  # before the values touch the page
        try:
            answer = await self.manager.run(
                page.session,
                lambda p: p.evaluate(fill_expression(expected, fills)),
                label="vault fill",
                dialog_aware=False,
            )
        except Exception as exc:
            # The exception text can quote the script, which holds the values: never shown.
            log.warning("vault fill failed: %s", type(exc).__name__)
            raise VaultError(
                scrub(f"The fill did not run ({type(exc).__name__}). Nothing was saved.", values)
            ) from None
        if isinstance(answer, str):
            with contextlib.suppress(ValueError):
                answer = json.loads(answer)
        return cast(dict[str, Any], answer) if isinstance(answer, dict) else {}

    # -- fill -------------------------------------------------------------------------------

    async def fill(
        self, scope: Scope, task_id: str, handle: str, broker: ServerRequestPort | None
    ) -> str:
        if not handle:
            return fail("fill needs a handle from browser_vault action=list.")
        backend = self._backend_for(scope, handle)
        if backend is None:
            return fail(f"No vault item with handle {handle!r}. Use browser_vault action=list.")
        if backend.needs_unlock and not backend.unlocked(scope):
            unlocked = json.loads(await self.unlock(scope, backend.name, broker))
            if not unlocked.get("success"):
                return as_json(unlocked)
        meta = next((m for m in await backend.items(scope) if m.handle == handle), None)
        if meta is None:
            return fail(f"No vault item with handle {handle!r}. Use browser_vault action=list.")
        if not meta.origins:
            return as_json(
                {
                    "success": False,
                    "error_type": "no_origin",
                    "error": f"Vault item {handle!r} is not bound to a site; it cannot be filled.",
                }
            )
        page = self._page(task_id)
        if isinstance(page, str):
            return page
        if page.origin not in meta.origins:
            return as_json(
                {
                    "success": False,
                    "error_type": "origin_mismatch",
                    "error": f"Refused: the page origin ({page.origin}) is not the item's "
                    f"origin ({', '.join(meta.origins)}). Vault fills run only on the exact "
                    "site the item was saved for.",
                }
            )
        if meta.kind == "payment" and not await _approve(
            broker,
            f"Fill payment card '{meta.label}' on {page.origin}",
            "The agent wants to enter your saved card into this checkout page. The card number "
            "and CVC never enter the conversation. Approve only if you intend to pay here.",
        ):
            return as_json(
                {
                    "success": False,
                    "error_type": "payment_declined",
                    "error": "The user did not confirm this payment card. Do not retry; ask them.",
                }
            )
        _nonce, controls = await self._controls(page)
        targets = [(c["stamp"], classify(meta.kind, c)) for c in controls if "stamp" in c]
        slots = [(stamp, name) for stamp, name in targets if name is not None]
        if not slots:
            return fail(f"No {meta.kind} fields were found on the current page.")
        secret = await backend.secret(scope, handle)
        try:
            fills: list[tuple[str, str]] = []
            used: set[str] = set()
            for stamp, name in slots:
                value = secret.get(name, "")
                if not value or (meta.kind == "login" and used):
                    continue
                fills.append((str(stamp), value))
                used.add(name)
            if not fills:
                return fail(f"No fillable {meta.kind} field matched the saved item on this page.")
            answer = await self._write(page, page.origin, fills)
        finally:
            values = list(secret.values())
            secret.clear()
        if answer.get("refused") == "origin_changed":
            return as_json(
                {
                    "success": False,
                    "error_type": "origin_changed",
                    "error": "Refused: the page left "
                    f"{page.origin} before the fill ran. Nothing was written.",
                }
            )
        filled = int(answer.get("filled") or 0)
        body: dict[str, Any] = {
            "success": filled > 0,
            "filled_fields": filled,
            "backend": backend.name,
            "kind": meta.kind,
            "origin": page.origin,
        }
        if meta.kind == "login":
            body["next"] = (
                "Submit the form. If the site asks for a verification code, call browser_vault "
                "action=enter_code with this handle."
            )
        else:
            body["fields"] = sorted(used)
        return scrub(as_json(body), values)

    # -- save_login / enter_code ------------------------------------------------------------

    async def save_login(
        self, scope: Scope, task_id: str, label: str, broker: ServerRequestPort | None
    ) -> str:
        page = self._page(task_id)
        if isinstance(page, str):
            return page
        if broker is None:
            return as_json(
                {
                    "success": False,
                    "error_type": "prompt_unavailable",
                    "error": "This session cannot ask the user for a login. Ask them to run "
                    f"`indus vault add` for {page.origin}.",
                }
            )
        host = page.origin.split("://", 1)[1]
        identifier = await _ask_text(
            broker, "identifier", f"Your username or email for {host} (it will be saved)"
        )
        password = (
            await _ask_secret(broker, "INDUS_VAULT_PASSWORD", f"Password for {host} (hidden)")
            if identifier
            else None
        )
        if not identifier or not password:
            return as_json(
                {
                    "success": False,
                    "error_type": "save_declined",
                    "error": "The user chose not to save a login for this site. Do not ask "
                    "again this turn.",
                }
            )
        try:
            meta = self.local.add(
                scope,
                kind="login",
                label=label or host,
                origin=page.origin,
                secret={"password": password},
                identifier=identifier,
            )
        finally:
            values = [password]
            del password
        if self.local.locked(scope):
            await self.local.unlock(scope, "")  # the user just typed it in: open for this process
        filled = json.loads(await self.fill(scope, task_id, meta.handle, broker))
        body = {
            "success": True,
            "handle": meta.handle,
            "origin": page.origin,
            "identifier": meta.identifier,
            "identifier_type": meta.identifier_type,
            "fill": filled,
            "next": "Type the identifier into the username field if the form has one, then submit.",
        }
        return scrub(as_json(body), values)

    async def enter_code(
        self, scope: Scope, task_id: str, handle: str, broker: ServerRequestPort | None
    ) -> str:
        page = self._page(task_id)
        if isinstance(page, str):
            return page
        _nonce, controls = await self._controls(page)
        boxes = [c for c in controls if "stamp" in c and classify("otp", c) is not None]
        if not boxes:
            return as_json(
                {
                    "success": False,
                    "error_type": "no_code_field",
                    "error": "No one-time-code field on this page. If the site wants an approval "
                    "on a device, tell the user to complete it there.",
                }
            )
        code: str | None = None
        source = "user"
        backend = self._backend_for(scope, handle) if handle else None
        if backend is not None and backend.unlocked(scope):
            with contextlib.suppress(VaultError):
                code = await backend.otp(scope, handle)
            if code:
                source = backend.name
        if not code:
            if broker is None:
                return as_json(
                    {
                        "success": False,
                        "error_type": "prompt_unavailable",
                        "error": "The page asks for a one-time code and this session cannot ask "
                        "the user. Save an authenticator key with the login to fill it.",
                    }
                )
            typed = await _ask_secret(
                broker, "INDUS_VAULT_CODE", f"One-time code for {page.origin} (hidden)"
            )
            code = (typed or "").strip().replace(" ", "").replace("-", "")
            if not code:
                return as_json(
                    {
                        "success": False,
                        "error_type": "code_declined",
                        "error": "The user did not enter a code. Do not ask again this turn.",
                    }
                )
        singles = [c for c in boxes if int(c.get("maxlength") or 0) == 1]
        if len(singles) >= len(code) > 1:
            fills = [(str(c["stamp"]), digit) for c, digit in zip(singles, code, strict=False)]
        else:
            fills = [(str(boxes[0]["stamp"]), code)]
        try:
            answer = await self._write(page, page.origin, fills)
        finally:
            values = [code]
            code = None
        if answer.get("refused") == "origin_changed":
            return as_json(
                {
                    "success": False,
                    "error_type": "origin_changed",
                    "error": "The page moved before the code was entered. Nothing was written.",
                }
            )
        filled = int(answer.get("filled") or 0)
        body = {
            "success": filled > 0,
            "filled_fields": filled,
            "origin": page.origin,
            "source": source,
            "next": "Submit the form (many sites submit when the last digit lands).",
        }
        return scrub(as_json(body), values)


# -- asking the user ---------------------------------------------------------------------------


async def _approve(broker: ServerRequestPort | None, command: str, description: str) -> bool:
    if broker is None:
        return False
    try:
        answer = await broker.ask(
            "approval",
            {"command": command, "description": description, "choices": ["once", "deny"]},
            timeout=PROMPT_TIMEOUT_S,
        )
    except RequestNotAnswered:
        return False
    return isinstance(answer, ApprovalAnswer) and answer.choice != ApprovalChoice.DENY


async def _ask_secret(broker: ServerRequestPort, name: str, prompt: str) -> str | None:
    try:
        answer = await broker.ask(
            "secret", {"name": name, "prompt": prompt}, timeout=PROMPT_TIMEOUT_S
        )
    except RequestNotAnswered:
        return None
    return answer.value if isinstance(answer, SecretAnswer) and answer.value else None


async def _ask_text(broker: ServerRequestPort, key: str, question: str) -> str | None:
    try:
        answer = await broker.ask(
            "clarify", {"questions": [{"id": key, "text": question}]}, timeout=PROMPT_TIMEOUT_S
        )
    except RequestNotAnswered:
        return None
    if not isinstance(answer, ClarifyAnswer):
        return None
    picked = answer.answers.get(key) or []
    text = picked[0].strip() if picked else ""
    return text or None


ACTS = ToolTraits(mutating=True)


def register_vault_tool(registry: ToolRegistry, manager: SessionManager) -> VaultTool:
    """Register ``browser_vault`` over the browser tools' session manager."""
    tool = VaultTool(manager)
    registry.register(
        TOOL_NAME,
        TOOLSET,
        SCHEMA,
        tool.__call__,
        probe=tool.ready,
        is_async=True,
        traits=ACTS,
    )
    return tool
