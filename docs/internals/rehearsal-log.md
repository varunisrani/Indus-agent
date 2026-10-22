# Canary dress rehearsal log (P25-22, P25-23)

The record of the rehearsal before `v1.0.0`: the P23 release workflow run in canary mode on the
candidate commit (dry run first, then for real), the update from the previous canary on every OS,
and the post-release smoke on the rehearsal's artefacts. **The owner fills this in**: the real run
needs the signing secrets, GHCR and three machines, and agents never start a real release (skill
reference 08 §1). Agents fill only §1 (what CI and the release lane prove without the owner).

Placeholders stay in angle brackets until the run; never invent a value. A row is done when its
Outcome column says `pass`, `fail (<finding id>)` or `n/a (<why>)`.

## 0. The run

| Field | Value |
|---|---|
| Candidate commit (on `main`) | `<sha>` |
| Fresh-clone gates (P25-21) | `<date>`, every gate `exit=0` (`scripts/release/release_gates.sh` output in HANDOFF.md) |
| Previous canary (installed on the rehearsal machines) | `<X.Y.Z-canary.YYYYMMDDTHHMMSSZ>` |
| New canary (this rehearsal) | `<X.Y.Z-canary.YYYYMMDDTHHMMSSZ>` |
| Release base (D-16.690) | `<owner/name>` |
| Operator, date | `<name>`, `<YYYY-MM-DD>` |
| Release workflow run | `<URL of the release.yml run>` |

Before the real run (each a yes with a date):

- [ ] the protected `release` environment exists with the signing secrets, by name: APPLE_ID,
  APPLE_TEAM_ID, APPLE_APP_SPECIFIC_PASSWORD, MAC_CERTIFICATE, MAC_CERTIFICATE_PASSWORD,
  WIN_CERTIFICATE, WIN_CERTIFICATE_PASSWORD (P23-15/16 must have landed for them to be used);
- [ ] GHCR: the package namespace of the release base exists and the workflow may push to it;
- [ ] the contributor map covers every author since the last release (`release.py` names gaps);
- [ ] the bootstrap mirror release carries the pinned uv and portable Git files (P23 owner item);
- [ ] the installers are attached to releases (see finding F-1 in §5: today they are not).

## 1. What runs without the owner (agents; CI)

| Check | Where | Last result |
|---|---|---|
| Canary source install → next stable, data intact (two locally built releases, offline) | `tests/e2e/upgrade/test_canary_to_stable.py` (release lane: `INDUS_RELEASE_LANE=1`) | pass, 2026-10-22, wave-44 builder R2 (25 s on a Linux container; base `bee44d9` + the wave's changes) |
| The smoke script's post-install steps on a locally built release | `tests/e2e/upgrade/test_smoke_steps.py` (release lane) | pass, 2026-10-22 (dashboard-login fails there by design: no `apps/` in a test release) |
| Smoke scripts: syntax, dry run, usage errors, release base, rollback sheet | `tests/unit/release/test_smoke_scripts.py` | pass, 2026-10-22; shellcheck and PSScriptAnalyzer were not installed (shellcheck runs when present; PSScriptAnalyzer and pwsh parsing are owed on a Windows runner) |
| Previous stable → candidate with the real installer | release.yml `upgrade` job | `<run URL>`: `<pass/fail>` |
| Fresh installs on three OSes | release.yml `install-e2e` matrix | `<run URL>`: `<pass/fail>` |

## 2. Dry run, then the real canary

Dry run (reads local refs only; prints the plan, the claim and the notes):

```bash
uv run python scripts/release/release.py --canary --commit <sha> --dry-run
```

| Step | Expected | Outcome |
|---|---|---|
| checks 1–4 | every line ✓ (a canary only warns about unmapped authors) | `<pass/fail>` |
| planned version | `<next patch>-canary.<stamp>`, attempt `rc.1-v<that>` | `<printed version>` |
| lanes | ci, docker, nix, install-e2e, desktop-mac, desktop-win, desktop-linux, upgrade | `<printed lanes>` |

Real run: dispatch `canary.yml` on `main` (or `release.py --canary --autopublish --commit <sha>`).

| Job (release.yml) | Outcome | Duration | Notes |
|---|---|---|---|
| admit | `<>` | `<>` | |
| ci (full suite incl. the release lane, guards, strict acceptance/traps) | `<>` | `<>` | |
| docker (built, tested) / docker-publish (pushed by digest) | `<>` | `<>` | digests: `<product>`, `<product-browser>`, `<sandbox>`, `<sandbox-browser>` |
| nix | `<>` | `<>` | |
| install-e2e (ubuntu, macos, windows) | `<>` | `<>` | |
| desktop-mac / desktop-win / desktop-linux (signed?) | `<>` | `<>` | artefact names: `<>` |
| upgrade | `<>` | `<>` | |
| upload (manifest) | `<>` | `<>` | |
| gate | `<>` | `<>` | |
| publish (receipt, `canary` aliases, channel record, public last) | `<>` | `<>` | |

Signing, per package (P23-15/16; R08.22 signing order):

| Package | Check | Outcome |
|---|---|---|
| macOS DMG / app | `codesign --verify --deep --strict`, `spctl --assess --type execute -vv`, notarisation ticket stapled | `<>` |
| Windows NSIS installer / app | Authenticode `Valid`, SmartScreen shows the signer | `<>` |
| Linux AppImage | runs, updates in place | `<>` |
| Licences | `THIRD_PARTY_NOTICES.md` inside every package (the smoke's `licences` step) | `<>` |

## 3. Update from the previous canary, per OS (P25-22)

On each machine the previous canary is installed and in use: at least two sessions with turns, a
key in the OS keychain (`indus auth add <provider>`), a second profile (`indus profile create
work`), the `indus` launcher on PATH, and for the desktop app a signed-in window with a session.

Source installs (`install.sh` / `install.ps1`, channel `canary`):

```bash
indus update --check            # exit 10: "Update available on canary: <new canary>"
indus update --plan             # channel canary, channel_source saved, from <previous>, to <new>
indus update -y
indus update --check            # "canary: already on <new canary>."
indus sessions list --limit 5   # the earlier sessions; resume one: indus -r <id>
indus auth list                 # the keychain credential is listed and works (indus doctor)
indus profile list              # work is there
command -v indus                # (Windows: Get-Command indus) the launcher still works
```

| OS (version) | Install kind | Previous → new | Offered without a channel switch | Version reported after | Sessions | Keychain secret | Profiles | Launcher | Outcome |
|---|---|---|---|---|---|---|---|---|---|
| Linux (`<distro>`) | source (`install.sh`) | `<> → <>` | `<yes/no>` | `<indus update --check line>` | `<n kept, resume ok>` | `<ok>` | `<ok>` | `<ok>` | `<>` |
| macOS (`<version, arch>`) | source (`install.sh`) | `<> → <>` | `<>` | `<>` | `<>` | `<>` | `<>` | `<>` | `<>` |
| Windows (`<version>`) | source (`install.ps1`) | `<> → <>` | `<>` | `<>` | `<>` | `<>` | `<>` | `<>` | `<>` |

Desktop app (its updater; a canary build reads the stable feed too):

| OS | Previous → new | Offered without a channel switch | Installed and relaunched; About shows the new version; no "update did not apply" | Sessions, settings, keychain items, CLI shim intact | Outcome |
|---|---|---|---|---|---|
| macOS | `<> → <>` | `<>` | `<>` | `<>` | `<>` |
| Windows | `<> → <>` | `<>` | `<>` | `<>` | `<>` |
| Linux (AppImage) | `<> → <>` | `<>` | `<>` | `<>` | `<>` |

## 4. Post-release smoke on the rehearsal artefacts (P25-23)

On a clean machine or VM per OS, against the new canary's public downloads:

```bash
bash scripts/release/smoke/linux.sh --tag v<new canary> --interactive
bash scripts/release/smoke/macos.sh --tag v<new canary> --interactive
powershell -ExecutionPolicy Bypass -File scripts\release\smoke\windows.ps1 -Tag v<new canary> -Interactive
```

Paste each summary (the `<step> PASS|FAIL|SKIP <detail>` lines) under its OS:

| OS | Summary | Failed steps → finding |
|---|---|---|
| Linux | `<paste>` | `<>` |
| macOS | `<paste>` | `<>` |
| Windows | `<paste>` | `<>` |

## 5. Findings

Every failure or surprise gets a row; its triage id comes from the release checklist
(`docs/internals/release-checklist.md`, RM/RS/RL).

| Id | Found by | What | Triage |
|---|---|---|---|
| F-1 | P25-23 builder (reading release.yml, 2026-10-22) | The release workflow uploads only the desktop artefacts: `install.sh`, `install.ps1` and `install.cmd` are not attached to releases, yet the install guide and `install.cmd` download them from `releases/latest/download/…`. The smoke's `install` step fails until they are attached (for example: copy them from the admitted commit into the uploaded artefacts). | RM30 (done at the wave 44 integration, D-16.960) |
| F-2 | P25-23 builder | The desktop app has no licences screen; the notices file ships inside each package (the smoke checks the file). | RS23 |
| F-3 | P25-23 builder | A script install has no built dashboard pages: `indus dashboard` builds them on first launch only when pnpm and Node are on the machine, otherwise only the API answers (`dashboard-login` fails). | RS24 |
| F-4 | P25-22 builder | A source install's `indus --version` prints the source tree's `0.0.0` (releases stamp the version into build trees only); the release it is on shows in `indus update --check` / `--plan`. | RS25 |
| F-5 | P25-23 builder | Without a terminal, `indus chat -q` refuses a risky command when run `--local` (`approvals.single_query_mode`) but, attached to the daemon, leaves it waiting for an approval ("Resume the session in a terminal to answer it") and exits 3. Both are safe; the smoke accepts either. Decide whether the two should agree. | RS26 (lead) |
| `<F-n>` | `<>` | `<>` | `<>` |

## 6. Sign-off

"Canary rehearsal done on `<OSes>` at `<sha>`: `<previous canary>` updated cleanly to `<new
canary>`; smoke `<n>` passed / `<n>` failed (findings `<ids>`)." — `<owner>`, `<date>`.
