# Task: `initialize` and `authenticate` (P19-02) Phase: 19 Owner: wave-29 builder A1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `acp_adapter/gateway_server.py`
(`initialize`, `authenticate` delegation, raw-message observer for elicitation),
`acp_adapter/server.py` (`initialize`, `authenticate`), `acp_adapter/auth.py` (all). Vendor schema:
the pinned SDK's generated `acp/schema.py` and `acp/meta.py` (schema tag v1.19.0). Our
`providers/route.resolve`, `providers/profile.ProviderProfile.needs_key`. Closed before writing.
Evidence: none.

What it does (own words): the editor's first call. We answer with the SDK's protocol version, who
we are (`agentInfo {name: "indus-agent", version}`), what we can do (`loadSession: true`,
`promptCapabilities.image: true`, `sessionCapabilities {fork, list, resume}`), and how the user can
sign in (`authMethods`):

- when the profile's route resolves with a usable credential (a key, or a provider that needs no
  key — a local endpoint or an identity-based login), an agent-managed method whose `id` is the
  provider's name;
- always the terminal method `indus-setup` (`type: "terminal"`, `args: ["--setup"]`), so a fresh
  editor install can run setup first.

The raw `initialize` params are read before any typed parsing: when
`clientCapabilities.elicitation.form` is present the client can show form elicitations (P19-11
uses it). `authenticate(methodId)`: `indus-setup` succeeds only once a credential resolves (setup
ran out of band); any other id must equal the resolved provider's name (case-insensitive); anything
else fails with JSON-RPC `-32000` ("authentication required"). Credential detection never raises
(a broken config is "no credentials").

Inputs → outputs: `initialize {protocolVersion, clientCapabilities, clientInfo}` →
`{protocolVersion: 1, agentInfo, agentCapabilities, authMethods}`; `authenticate {methodId}` → `{}`
or error `-32000`.

Files: `src/indus/acp/agent.py` (dispatcher + initialize/authenticate), `src/indus/acp/auth.py`,
`tests/contract/acp/test_initialize.py`, `tests/fakes/acp_client.py`.

Vendor names used (checked against `acp/schema.py` of agent-client-protocol 0.12.1, schema
v1.19.0): methods `initialize`, `authenticate` (stable); `InitializeResponse.protocolVersion`,
`agentInfo{name,version}`, `agentCapabilities{loadSession, promptCapabilities{image},
sessionCapabilities{fork, list, resume}}`, `authMethods[]` (`AuthMethodAgent{id,name,description}`,
`TerminalAuthMethod{type:"terminal", id, name, description, args}`), `ClientCapabilities.elicitation
{form, url}`; error code `-32000` (`RequestError.auth_required`).

Edge cases / failure modes:
- No credentials: only `indus-setup` listed; authenticating with it fails until a key exists.
- Provider name casing/whitespace in `methodId`: normalised.
- Non-string `methodId`: invalid params (`-32602`).
- `initialize` sent twice: answered again (stateless), elicitation flag re-read.
- `elicitation: {}` without `form`: no form support.

Our design differences / decisions:
- Version from `indus.__version__`.
- `authenticate` failure is a JSON-RPC error (the SDK's `auth_required`), not a null result: a null
  result is not a valid `AuthenticateResponse` in the pinned schema.

Tests we will write:
- capabilities exact, and validated through the SDK's `InitializeResponse` model;
- no credentials → only `indus-setup`; authenticate fails; add a fake key → provider listed and
  both methods succeed; other ids fail;
- elicitation flag from raw params (with/without `form`).

## Decisions for the lead
- Failure answer of `authenticate` = `-32000` error (see above).

Compare checklist (filled after the rewrite; reopened `acp_adapter/auth.py`, `server.py`
`initialize`/`authenticate`, `gateway_server.py` observer):
- [x] capability set
- [x] auth method list and setup method
- [x] authenticate rules
- [x] raw elicitation detection
- Deliberate differences: see above.

## Status

Done (2026-10-20), wave-29 builder A1. Tests: see the task's Verify line; all green with the gate checks. Trap/ACC ledger rows for R09.9/R09.13/R09.14/R09.16 and ACC-M1/M2/M8 left to the lead (follow-up).
