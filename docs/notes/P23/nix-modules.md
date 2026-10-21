# Task: NixOS and home-manager modules (P23-14) Phase: 23 Owner: wave-36 builder N1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `nix/nixosModules.nix` (option
declarations and the assertion list only), `nix/moduleCommon.nix` (option declarations and their
descriptions). Closed before writing.
Evidence: none (packaging task).

What it does (own words): a NixOS host runs the Indus gateway as a service declared in its
configuration; a home-manager user gets `indus` on PATH, a declared config and, if asked, the
gateway as a per-user service. The configuration is declarative: the module writes `config.yaml`
from a Nix attribute set and marks the home as managed (`<home>/.managed`), so `indus config set`,
`indus setup` and the dashboard's config writes refuse with "this installation is managed"
(already built in P01/P07/P18). Secrets never enter the Nix store: they come from runtime env
files (sops-nix, agenix) that systemd reads as root before dropping privileges.

Inputs → outputs:
- `nix/module-common.nix` (`{ lib, defaultPackage }` → shared `options` + helpers): `enable`,
  `package`, `workingDirectory` (written as `terminal.cwd`), `configFile` (a whole `config.yaml`,
  used verbatim), `settings` (attribute set, deep-merged across modules, rendered to YAML),
  `environmentFiles` (runtime paths as strings, never store paths), `environment` (non-secret),
  `extraPackages` (on the service PATH), `extraPythonPackages` (on `PYTHONPATH` so the
  `indus.plugins` entry points of extra packages are found), `mcpServers` (typed entries rendered
  under `mcp_servers.<name>`), `extraArgs` (after `indus gateway run`), `restart` and
  `restartSec`; read-only `renderedConfig` (what lands in `config.yaml`, inspected by the checks).
- `nix/render.nix` (builtins only, testable with `nix-instantiate --eval`): render one MCP server
  (drop unset fields, keep the free-form `extraConfig` keys), the full config (`settings` + the
  config stamp + `terminal.cwd` + rendered `mcp_servers`, declared servers win by name), and the
  problem lists the assertions report.
- `nix/nixos-module.nix` (`services.indus-agent`): `user`/`group` (`indus-agent`), `createUser`,
  `stateDir` (`/var/lib/indus-agent`, the `INDUS_HOME`), `addToSystemPackages`, `native.enable`
  (default: on unless container mode), `container.{enable, backend (docker|podman), image,
  extraOptions, extraVolumes}`. A oneshot `indus-agent-setup` unit writes `config.yaml` and the
  managed marker before the gateway starts. Native: a hardened systemd unit
  `indus-agent.service` running `indus gateway run`, `EnvironmentFile=` for secrets, the service
  user lingers (so it has a user manager for per-user units and timers). Container: the same
  gateway through `virtualisation.oci-containers` (host network, `stateDir` at `/opt/data`, the
  image's own UID/GID remap from `INDUS_UID`/`INDUS_GID`).
- `nix/home-manager-module.nix` (`programs.indus-agent`): `home` (`~/.indus`), `installPackage`,
  `gateway.enable`; config and marker written by an activation step only when `settings`,
  `mcpServers` or `configFile` are set (otherwise the home stays user-managed); the gateway as a
  systemd user service on Linux and a launchd agent on macOS.
- `nix/module-checks.nix`: evaluation cases under `checks.<linux system>`: native default
  evaluates; container + native refused; container without an image refused; `configFile` with
  `settings` refused; a server with both `command` and `url` refused; settings rendered (with the
  stamp and `terminal.cwd`); MCP entries rendered; the home-manager module renders the same
  config and declares the user service.

Files: `nix/module-common.nix`, `nix/render.nix`, `nix/nixos-module.nix`,
`nix/home-manager-module.nix`, `nix/module-checks.nix`, tests in
`tests/unit/packaging/test_nix_files.py` (shared with P23-13), this note.

Edge cases / failure modes:
- both modes on → assertion "native and container mode are both enabled; pick one";
  neither on while enabled → assertion (nothing would run);
- container mode with `extraPackages`/`extraPythonPackages` → a warning (the image has its own
  environment);
- `configFile` and non-empty `settings`/`mcpServers` together → assertion (one source of truth);
- an MCP server needs exactly one of `command` (stdio) or `url` (http/sse); `transport` only with
  `url`;
- env files are strings, so a secret is never copied into the world-readable store; `environment`
  is documented as non-secret;
- the home is owned by the service user, mode 0700 (tmpfiles); config written atomically (temp +
  rename) so a crash never leaves half a file;
- the native unit's PATH starts with bash, coreutils and git (what the agent's terminal tools
  expect on any host), then `extraPackages`;
- home-manager: a `.managed` marker written while settings were declared stays after the user
  removes them (the module cannot tell its marker from one an administrator wrote); delete it by
  hand;
- home-manager on macOS: no `EnvironmentFile=`, so the launchd agent runs a small wrapper that
  loads the env files (`set -a; . file`) and then execs the gateway.

Our design differences / decisions:
- Our names: `services.indus-agent` / `programs.indus-agent`, user `indus-agent`, home
  `/var/lib/indus-agent`.
- The module owns `config.yaml` outright and marks the home managed, instead of merging into a file
  that runtime tools also edit: our config writer already refuses managed homes, so a merge would
  only hide edits that are overwritten at the next start.
- Secrets reach the process through systemd `EnvironmentFile=` (native) or the container's env
  files, not by rewriting `<home>/.env`.
- Two explicit mode switches (`native.enable`, `container.enable`) with an assertion, rather than
  an enum, so a host that turns container mode on keeps working and a contradictory config is a
  clear evaluation error.
- No dashboard/backend service, documents, plugins-by-directory, OAuth seeding or host-user
  passthrough options in 1.0 (see "Not taken").

Tests we will write:
- every `.nix` file parses; `render.nix` evaluated with `nix-instantiate` (skipped without Nix):
  settings rendered with stamp and cwd, MCP entries rendered without unset fields, problem lists
  for each assertion case;
- every typed MCP field the module declares exists on `indus.mcp.config.McpServer`;
- shared option names declared once in `module-common.nix` and used by both modules;
- `nix/module-checks.nix` names one check per assertion case.

## Decisions for the lead
- `container.image` has no default: the GHCR owner is open (D-16.690 / P25-20). When it is fixed,
  set the default from the same single constant the installers read (a generated value in
  `nix/pins.json` is the place).
- Rendering uses `builtins.toJSON` (JSON is YAML 1.2, which ruamel.yaml reads) so the renderer has
  no nixpkgs dependency and is testable offline.
- Lingering is on by default in native mode (`users.users.<user>.linger`), off in container mode.

Compare checklist (filled after the rewrite; reopened the option declarations of
`nix/nixosModules.nix` and `nix/moduleCommon.nix`):
- [x] shared options present (package, working dir, config file, settings, env files, environment,
  extra packages, extra python packages, MCP servers, restart policy) (test:
  `test_nix_files.py::test_common_options_declared`)
- [x] MCP server fields match our schema (test: `::test_mcp_fields_exist_in_schema`)
- [x] container + native refused (test: `::test_render_problems_for_modes`; check
  `module-container-and-native-refused`)
- [x] settings and MCP entries rendered (tests: `::test_render_config`, `::test_render_mcp_server`)
- [x] lingering only in native mode (check `module-native-default`)
- Deliberate differences: the reference deep-merges declared keys into an on-disk file that runtime
  tools edit, seeds `.env` from env files during activation, and has a dashboard/backend service
  with network wait logic, document trees, directory plugins and OAuth file seeding; we own the
  file and mark the home managed, pass secrets through systemd, and leave the rest out of 1.0.
- Not taken: documents/home files, directory plugins, auth file seeding, container host-user
  passthrough, backend (dashboard) unit — candidates for a later wave if users ask.

## Status

Partial (2026-10-21): modules, renderer and evaluation checks written; renderer evaluated offline
with `nix-instantiate`; the module evaluation checks (ACC-S4-2) need nixpkgs and home-manager and
run in the Nix lane.

## Integration (wave 36)

Integration: `container.image` defaults to `${pins.image}:stable`, where `nix/pins.json` `image` is generated from `RELEASE_REPOSITORY` by `scripts/nix/pins.py` (D-16.740, D-16.758); the `container-needs-image` check sets `image = null` explicitly.
