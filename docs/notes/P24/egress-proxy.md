# Task: Egress credential-injection proxy for Docker sandboxes (P24-42) Phase: 24 Owner: wave-38 builder S1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/environments/docker_egress.py`
(all: posture args, collision guards, env merge), `agent/proxy_sources/iron_proxy.py` (behaviour
only: status, CA, token mappings, provider host table shape, config builder, daemon start/stop,
subprocess env), `hermes_cli/proxy_cli.py` (the subcommand list and flags only); the egress lines of
`tools/environments/docker.py` (label filter on reuse, grep only). Closed before writing.
Research: `docs/plan/research/02-tools.md` §3.10 Docker (egress bullet, reuse labels), R02.20.
Evidence: none.

What it does (own words): with `egress.enabled`, a Docker sandbox never receives a real provider
key. The host runs a small forward proxy of ours (`indus egress serve`, a detached process started
on demand). Each new container gets: `HTTPS_PROXY`/`HTTP_PROXY` (both casings) pointing at the
proxy through `host.docker.internal` (`--add-host host.docker.internal:host-gateway`), loopback-only
`NO_PROXY`, a read-only CA mount plus CA-bundle variables (`SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`,
`CURL_CA_BUNDLE` → a bundle of the public roots plus our CA; `NODE_EXTRA_CA_CERTS` → our CA alone),
`INDUS_EGRESS=1`, and one opaque per-container token under every provider key name that has an
injection rule. The proxy intercepts TLS only for allow-listed hosts (a leaf certificate per host,
signed by our CA); inside an intercepted request it swaps the token — in the headers the rule
names, and in the query string — for the real secret read from the profile's `SecretStore` at that
moment. Other hosts are tunnelled byte for byte (never decrypted, the token passes unchanged and is
useless there), or refused with `egress.block_other_hosts`. Upstream addresses that are not public
(loopback, private, link-local, metadata, CGNAT …) are refused whatever the host name says.

Rules: built-in rules come from our provider catalogue (`providers.bundled.PROFILES`): each
`https` base URL's host, its first key name, and the header the API reads (`Authorization` for
OpenAI-style, `x-api-key` + `Authorization` for Anthropic-style, `x-goog-api-key` for Gemini). A
rule is live only while its secret resolves. `indus egress allow <host> [--secret NAME] [--header H]`
adds a host (wildcards `*.example.com` allowed) and optionally a user rule.

Posture and reuse (R02.20): every container carries `indus-egress=<posture>` (`off`, or a digest of
proxy URL, CA fingerprint, mounts and injected names — never a token). Reuse filters on it, so a
container made without the proxy is never reused with it, nor one made with it after `disable`.

Enforcement (`egress.enforce`, default on): `terminal.docker_forward_env`, passthrough names and
`terminal.docker_env` entries that name a proxy/CA variable or an injected name, and
`terminal.docker_extra_args` that set such a variable (`-e`, `--env`, short-flag chains like
`-ite`, `--env=`), any `--env-file`, or a `--network` flag, refuse the start
(`EgressConflict`). With enforcement off the conflict is a warning and the user's setting wins. A
proxy that is enabled but cannot run (no CA library, daemon would not start) refuses the start
under enforcement and warns otherwise. `docker_network: false` means no egress at all: posture
`off`, nothing injected.

Inputs → outputs: `indus egress enable [--yes]` (config on, CA made, daemon started),
`disable` (config off, daemon stopped), `status [--json]` (state, daemon pid/address, hosts,
injected secret names — never values), `allow <host> [--secret NAME] [--header NAME]`,
`serve` (hidden, the daemon). `open_docker` → `EgressPlan` (mount args, env names + values, host
args, label) folded into `docker run`.

Files: `src/indus/backends/docker/egress.py`, `src/indus/cli/commands/egress.py`,
`tests/unit/backends/docker/test_egress.py`, `tests/integration/backends/test_egress_proxy.py`;
shared: `backends/docker/args.py` (posture helpers), `backends/docker/environment.py` (plan
folded in, label), `foundation/config/schema.py` (`egress.*`), `cli/app.py` (`egress` row), CLI
contract tables, `pyproject.toml` (`egress` extra = `cryptography`, already locked).

Edge cases / failure modes:
- daemon already running: `enable` and `open_docker` reuse it (health answer with the instance id
  written in `<home>/egress/proxy.json`); a stale record (pid gone or a different answer) is
  replaced; `disable` only signals a pid whose health answer matches (no pattern kill).
- token store holds SHA-256 digests only (`<home>/egress/tokens.json`, 0600); a token is minted when
  a container is created, not when one is reused (a reused container keeps its baked token).
- a request to an allow-listed host without a token is forwarded unchanged (nothing added).
- credentials are never logged, never in argv (`-e NAME` only, values in the client env), never in
  exception text; the proxy logs host, method and status only.
- bind address: `auto` = the Docker bridge gateway on Linux (probed), loopback elsewhere (Docker
  Desktop forwards `host.docker.internal` to the host loopback); never `0.0.0.0`.
- Windows/macOS: same flow; the daemon is detached with the platform's own session flags.

Our design differences / decisions:
- our own proxy (asyncio + `cryptography` for the CA and leaves) instead of a vendor binary: no
  extra download or licence question; TLS interception only for allow-listed hosts.
- per-container tokens (reference: one token per provider for every sandbox).
- non-listed hosts are tunnelled by default (reference: default deny); `egress.block_other_hosts`
  gives the deny behaviour. The CA bundle therefore holds the public roots too.
- no management API / hot reload: the daemon re-reads config and tokens when their files change.
- no Bitwarden credential source, no `setup` wizard, no `install`/`restart`/`reload`/`config` verbs.

Tests we will write:
- unit: plan off/on/network-off, label digest stable and token-free, env names only in argv,
  collisions (forward env, docker env, `-e`, `-ite`, `--env=`, `--env-file`, `--network`), enforce
  off → warning, reuse filter carries the posture (fake Docker client), container env has no
  provider key, rules from the catalogue, header swap and query swap, deny policy, token book.
- integration: proxy on loopback with fake TLS upstreams (resolver override): allow-listed host
  gets the real secret, a tunnelled host sees the token unchanged, unknown token not swapped,
  blocked host refused; daemon start → health → stop (process always stopped).

## Decisions for the lead
- Non-listed hosts tunnelled by default (`egress.block_other_hosts: false`); flip the default for
  the reference's default-deny posture.
- `indus egress enable` needs `--yes` without a terminal (it starts a background process).
- No NODE_OPTIONS change: `NODE_EXTRA_CA_CERTS` adds our CA for Node.
- Containers created before this change have no `indus-egress` label and are no longer reused
  (the orphan reaper removes them) — the posture filter applies to `off` too.

Compare checklist (filled after the rewrite; reopened `docker_egress.py`, `iron_proxy.py`, `proxy_cli.py`):
- [ ] posture label filter applies to every posture incl. off
- [ ] collision rules (forward env folded case, docker env, extra args chains, env-file, network)
- [ ] env names written by the posture are all critical
- [ ] proxy child env minimal; no proxy vars inherited by the daemon
- Deliberate differences: see above.
- Not taken: see above.

## Status

Done except the Compare step (2026-10-21, wrap-up requested): code, unit + loopback integration tests, CLI. Compare checklist NOT yet walked; `egress` extra (cryptography==50.0.2, already locked) not yet added to pyproject — lead to add. Shared edits: docker/environment.py, foundation/config/schema.py (+test), cli/app.py, CLI contract tables, tests/fakes/backend/docker_cli.py (`--add-host` value flag). args.py untouched (plan named it; the posture is folded in environment.py).
