# v1.0 release checklist

How Indus Agent 1.0 goes out, step by step, and the bug-bash list that has to be settled first. The
order is fixed: the one-time decisions, every gate green from a fresh clone, a canary dress
rehearsal on every operating system, the release (the owner's), then a smoke test on each OS.

**Tagging `v1.0.0` is the owner's decision.** Agents prepare and verify everything up to that point
and stop. They never create the tag or an attempt ref, never run `scripts/release/release.py`
without `--dry-run`, never dispatch `release.yml` or `canary.yml`, never promote a draft release,
move a GHCR alias or set a signing secret. A dry run is fine; the real thing is the owner's.

Related pages: `docs/notes/P23/` (installers, images, Nix, update, release workflow),
`.github/workflows/release.yml` (lanes; the signing secret names in a comment) and `canary.yml`, the threat model
(`docs/internals/threat-model.md`, wave 43), `docs/internals/acceptance-map.md`,
`docs/internals/traps-ledger.md`, `docs/internals/parity-report-v1.md`,
`docs/internals/similarity-report.md`, `docs/plan/16-what-we-change.md`, and the P25 notes
`docs/notes/P25/release-checklist.md` and `release-gates.md`.

Status when this list was written (2026-10-22, main `bee44d9`; last product commit `226a5ae`, wave
42): P00–P22 complete; P23 21/23 (P23-15/16 desktop signing open); P24 45/47 built and wired (21/22
after 1.0, D-16.15); P25-05..19 landed; P25-20..24
in wave 44. Updated at the wave 44 integration (on main `7d9a864`): wave 43 (P25-01..04) landed in
`4945b48`, so RM1 and RM3 moved to Done; the open wave-43 findings SEC-14 and SEC-15 and the
rehearsal findings F-1..F-5 (`docs/internals/rehearsal-log.md`) were added. Updated at the
waves 45 + 46 integration (2026-10-22, D-16.1023): RM2, RM4, RM8, RM21, RM28, RM29 and RS4, RS6,
RS7, RS9, RS10, RS11, RS16, RS19, RS23–RS26 moved to Done (landed in `44c9a4f`). Updated at the wave 47
integration (2026-10-22, D-16.1057): the six bugs seen while capturing screenshots recorded as
RS27–RS32 and moved to Done; RS24 and RS26's follow-ups closed; RS3 and RL26 partly done. Updated at
the wave 48 integration (2026-10-22, D-16.1100): RS3 moved to Done (desktop room view; TUI / web
room views are owner question D-16.1071); the screenshot pass's minor UI follow-ups recorded as
RS33–RS35 and the katex / sprintf-js advisories as RS36, all moved to Done; the pypdf re-lock
recorded as RS37; RS29, RS30, RL26 and RM25 updated. This file is checked by `python -m tooling.checks checklist` (ids, counts, Source and
Why, owner approvals, the After v1.0 list).

## 0. One-time decisions (owner)

Each decision fixes something for good. Until the owner answers, the placeholder below is in force;
it is chosen so that it cannot reach users by accident. Agents never fill these in.

- [ ] **Release base and GHCR namespace** (RM9, D-16.690). What hangs on it: every installer
      download URL, the update feed, the image names (`ghcr.io/<owner>/indus-agent`,
      `…-sandbox`), the Nix module's default image and the desktop's in-app installer. Options:
      keep `indusagi/indus-agent`, or publish from the repository the code lives in
      (`varunisrani/indus-agent`), or another organisation. Recommendation: an organisation
      account (it survives a change of maintainer). Placeholder: `RELEASE_REPOSITORY =
      "indusagi/indus-agent"` in `src/indus/foundation/versions.py` and its copy in
      `apps/desktop/electron/releaseBase.ts`; change it there, then `scripts/install/pins.py
      update`, `scripts/docker/pins.py` and `scripts/nix/pins.py` regenerate the copies
      (`tests/unit/packaging/test_release_base.py` holds them equal). Nothing is published until
      the owner runs the release.
- [ ] **Desktop app ids** (RM10). What hangs on it: macOS bundle identity and keychain items,
      Windows upgrade identity, the Linux launcher and AppStream id; a public canary install fixes
      it. Placeholder: `ai.indus.desktop` (`apps/desktop/electron-builder.config.cjs`,
      `apps/desktop/scripts/stage-app.mjs`, `apps/desktop/electron/linuxLauncher.ts`).
      Recommendation: keep it only if the owner controls the `indus.ai` domain; otherwise a
      reverse-domain id under a domain the project controls, decided before the first public
      canary.
- [ ] **Windows installer format**: settled — a signed NSIS installer built by electron-builder
      (Authenticode), MSIX/Store after 1.0 (D-16.70, D-16.15). Signing itself is RM14.
- [ ] **Docs domain** (RM11). What hangs on it: `site_url` in `mkdocs.yml` (unset today, so the
      `llms.txt` links are site-relative), the Pages deployment of `.github/workflows/docs.yml`,
      and every link printed by the CLI or the apps that points at the docs. Placeholder: no
      domain; links stay relative.
- [ ] **Maintainer contact** (RM12). What hangs on it: the security-report address in the docs,
      package metadata and the desktop's About box. Placeholder:
      `Indus Agent maintainers <maintainers@indus.invalid>` (the `.invalid` domain can never
      deliver mail); no package carries an address today.
- [ ] **Signing secrets** (RM14): created by the owner in the protected `release` environment,
      by name only: `APPLE_ID`, `APPLE_TEAM_ID`, `APPLE_APP_SPECIFIC_PASSWORD`,
      `MAC_CERTIFICATE`, `MAC_CERTIFICATE_PASSWORD`, `WIN_CERTIFICATE`,
      `WIN_CERTIFICATE_PASSWORD`. The desktop lanes pass no secret until P23-15/16 land.
- [ ] **Trademark search** for "Indus Agent" (RM13, D-16.40): the licence (MIT) is settled; the
      name search and counsel's review of the notices wording are open.
- [x] **REA registration** (D-16.61): settled — owner approved 2026-10-18; rea-agents 6.7.0.
- [ ] **Licence rulings** for components shipped under dated exceptions (RM15, RM16).
- [ ] **Open D-16 rows** (§3.2 of `16-what-we-change.md`): D-16.690 (RM9), D-16.801 and D-16.802
      (RS13), and the rows "accepted — lead … owner may override" (D-16.42, 44–48, 56–60, 62–64),
      which stand unless the owner overrides them before the tag.
- [ ] **Bug-bash list below**: every must-fix row is closed (commit in the Done table) or moved to
      should-fix with the owner's written approval quoted in the row with its date.

## 1. All gates green from a fresh clone (P25-21)

On the release commit, from a fresh clone (no dependencies, cache, build output or ignored files),
on a machine with Docker, Nix, a display server and the browsers:

```bash
INDUS_REF=<pinned reference checkout, outside the clone> \
PARITY_REF=<owner's reference captures, ~/rea-lab/parity/ref> \
WORK_DIR=<new empty folder> \
scripts/release/release_gates.sh <repo url> <candidate sha> scripts/release/gates.txt
```

The runner clones into `<work>/clone`, runs every gate in `scripts/release/gates.txt` even after a
failure, and writes `<gate> exit=<n> <s>s` lines to `<work>/gates.txt` and each gate's output to
`<work>/logs/<gate>.log`. Gates: install, lint, format, types, generated, unit, contract,
integration, security, upgrade (release-lane rehearsal), webe2e, tuie2e, desktope2e (packaged), cleanroom, acceptance (`--strict`),
traps (`--strict`), acpdrift, mcpdrift, bundle, perf, docs, notices, audit, parity, similarity,
installers, dockerimage, nix.

- [ ] Every gate `exit=0` in one run on the exact commit that gets the tag. A failure means: fix,
      commit, and start this section again from a new clone; any commit after a green run voids it.
- [ ] The run (each gate with its exit code and duration) is logged in `HANDOFF.md`.
- [ ] CI is green on that commit in every workflow (`ci.yml`, `docker.yml`, deps audit, docs), the
      performance budgets judged on the clean CI runners, not on a loaded workstation.
- [ ] The similarity top 20 is reviewed: every region has a verdict in
      `docs/internals/similarity-report.md` (RM17).
- [ ] A person ran one real turn per OS on the candidate with a real provider key: an approval, a
      file change with its diff, a stop, a resume.

## 2. Canary dress rehearsal (P25-22, owner)

- [ ] `scripts/release/release.py --commit <sha> --canary --dry-run`, then the real canary run
      (owner): every desktop target built, signed (macOS notarised, Windows signed) and validated
      with its licence files; images pushed by digest; installers attached to the prerelease.
- [ ] On macOS, Windows and Linux a machine with the previous canary installed and its data in
      place updates: `indus update` (source installs) or the desktop updater; the update is
      offered without switching channel; the new version is reported; sessions, keychain
      secrets, profiles and the launcher survive.
- [ ] `tests/e2e/upgrade/test_canary_to_stable.py` green in the release lane.
- [ ] Outcomes recorded in `docs/internals/rehearsal-log.md` (OS, versions, steps, results).

## 3. Release (owner)

- [ ] The owner approves the release commit and cuts it: `scripts/release/release.py --commit
      <sha>` pushes the attempt ref `rc.<n>-v1.0.0`, drafts the release and dispatches
      `release.yml`.
- [ ] Workflow order holds: admit → ci (incl. `acceptance traps --strict --require-ref`) → lanes
      (docker, nix, desktop macOS/Windows/Linux, upgrade, web-pages, upload) → gate →
      docker-publish (pushes the archives the docker lane tested) → publish (aliases moved to
      those digests, release published). Nothing is rebuilt for publication; a failure before
      publish leaves nothing announced.
- [ ] From a clean machine: the one-line installer prints `1.0.0` from `indus --version`; `docker
      run ghcr.io/<owner>/indus-agent:1.0.0 --version` prints `1.0.0`; the GitHub Release lists
      every installer, the checksums and the generated notes.

## 4. Post-release smoke and rollback (P25-23)

- [ ] Per-OS smoke scripts (`scripts/release/smoke/`) on a clean machine or VM, public downloads
      only: install, `--version`, `setup --non-interactive` against a fake-model endpoint, one
      turn with an approval, resume, `doctor`, desktop launch to chat, dashboard login on
      loopback, licences screen, uninstall keeping data.
- [ ] The README's install section and the user docs match what shipped.
- [ ] If a step fails badly, the owner rolls back as `docs/internals/rollback.md` says: the GitHub
      Release back to draft, GHCR aliases back to the previous digest, the channel record
      reverted, fix forward with `1.0.1`. Never delete or reuse a version.

## 5. Afterwards

- [ ] Similarity ranking re-run on the tag and recorded (commit, date, result).
- [ ] `HANDOFF.md` updated with "Ready for `v1.0.0` at `<sha>`" (written only after a green §1 run
      and a done §2), then the tag result; canaries resume as `1.0.1-canary.*`.
- [ ] Should-fix and later rows that did not make 1.0 moved to the issue tracker.

## Bug-bash triage

Collected on 2026-10-22 at `bee44d9` from every task note's follow-ups and open statuses
(`docs/notes/**`), the lead notes (`docs/plan/phases/LEAD-NOTES.md`), the open items of
`HANDOFF.md`, the Known gaps and Review rows of the acceptance map, the Review rows of the traps
ledger, the security findings of wave 43 (SEC-1..11 in the parked threat model, SEC2-1..4 in
`.agents/wip/w43/README.md`) and the parity ledger. Every must-fix and should-fix row was checked
against the code on that day (the check is in the row). Items recorded at or after wave 42
(`226a5ae`) are open by construction: only briefs and handoff commits landed since. Later rows
group older follow-ups by area; where a later row says "not re-checked", check it before working
it. Items found already fixed are listed at the end and not counted.

Buckets: **must-fix** (1.0 does not ship without it, or the owner must decide first),
**should-fix** (a real defect or gap with a workaround; fix before 1.0 if time allows), **later**
(after 1.0, or a deliberate scope cut recorded in `16-what-we-change.md`). The `R` prefix keeps
these ids apart from the inventory's S-ids.

Ids are permanent: never renumber or reuse one. An item that changes bucket keeps its id and moves
to the other table; an `RM` row moves to should-fix only with the owner's written approval quoted
in the row with its date; an id that turns out to be a duplicate goes to Withdrawn with a reason.

**Ids assigned:** RM1–RM30 · RS1–RS37 · RL1–RL26

### Must-fix for 1.0 (21)

| # | Item | Source | Why |
| --- | --- | --- | --- |
| RM5 | SEC-7 (non-http `Origin` accepted on loopback binds, `Origin: null` passes the origin check) and SEC-10 (outside text under 32 characters is not fenced) are accepted residuals awaiting the owner's written acceptance | threat model SEC-7, SEC-10 | Both are documented and limited (a credential is still required on every route; golden prompts unaffected), but only the owner can accept a residual risk for the release |
| RM6 | SEC-9: webhook `auth: auto` accepts an untimestamped generic HMAC, replayable after the one-hour delivery-id window | threat model SEC-9; `HANDOFF.md` owner decisions (3) and owner items | A captured delivery can be replayed later. Owner decides whether `auto` requires the timestamped form |
| RM7 | SEC2-2: certifi 2026.7.22 has a critical advisory (GHSA-6q48-6c7w-9gw2, emSign roots); the fixed 2026.10.19 is newer than the 14-day `exclude-newer` rule | threat model SEC-17 (SEC2-2); `uv.lock` (checked: certifi 2026.7.22) | The shipped trust store holds roots that were withdrawn; its allow-list entry expires 2026-11-08 and the audit then fails every build. Owner or lead: a dated exception or a moved `exclude-newer`, then re-lock |
| RM9 | Release base and GHCR namespace: `RELEASE_REPOSITORY = "indusagi/indus-agent"` while the code lives at `varunisrani/indus-agent` | D-16.690; `src/indus/foundation/versions.py:62`; `HANDOFF.md` owner items | Every download, image, update feed and the desktop installer derive from this value; it is fixed for every installed copy once released (§0) |
| RM10 | Desktop app id `ai.indus.desktop` (bundle id, Windows upgrade identity, Linux launcher id) needs the owner's confirmation | P25-20 row; `apps/desktop/electron-builder.config.cjs:19` | A public install fixes the id for good; changing it later strands keychain items, launchers and updates (§0) |
| RM11 | Docs domain: `site_url` unset in `mkdocs.yml`, Pages target not chosen | P25-14 note `docs/notes/P25/docs-site.md`; `mkdocs.yml:11` | Without it the published docs have no canonical address and the `llms.txt` links stay relative (§0) |
| RM12 | Maintainer contact: no monitored address chosen; placeholder `<maintainers@indus.invalid>` | P25-20 row; skill reference 08 §4 | Security reports and package metadata need a real, monitored address; the placeholder can never deliver (§0) |
| RM13 | Trademark search and counsel's review of the name and the notices wording | D-16.40; `HANDOFF.md` current state | Shipping under a name that conflicts with a mark is costly to undo after launch |
| RM14 | Desktop signing (P23-15/16) not built: macOS notarisation and Windows Authenticode lanes, the protected `release` environment and its secrets (by name in §0) | `docs/plan/phases/LEAD-NOTES.md` "P23 wave 36" (tasks, owner items); D-16.70 | Unsigned builds meet Gatekeeper refusals and SmartScreen warnings; the canary rehearsal (§2) needs signed builds |
| RM15 | Licence rulings for components shipped under dated exceptions: regex (CNRI-Python), pyarrow's bundled Zlib / BSL-1.0, coincurve (MIT OR Apache-2.0), Pillow (MIT-CMU), the voice extra's av (LGPL FFmpeg) and numpy (Zlib parts), Piper / eSpeak NG, sherpa-onnx's bundled libasound (LGPL, D-16.849), Git for Windows (GPL-2.0, source offer), the P21 vendor-extra transitives | `HANDOFF.md` owner items and owner decisions (3); `docs/notes/P25/notices.md`; `tooling/checks/licenses_allow.json` | Redistribution under a licence the owner has not accepted is a legal risk; each exception is dated and must be ruled on (keep, drop the component, or replace it) |
| RM16 | Notices for the shipped Docker images: the Debian package set is credited by one `LicenseRef-Debian-packages` row pointing at `/usr/share/doc`, Chromium in the browser variants is not listed; hadolint (GPL-3.0) is CI-only and never shipped | `docs/plan/phases/LEAD-NOTES.md` "P23 wave 36" owner items; `scripts/release/notices_data.json` (checked) | Images redistribute these binaries; the owner confirms that the in-image copyright files satisfy the obligations or asks for a generated list |
| RM17 | Similarity report: the 20 closest regions have no verdict | `docs/internals/similarity-report.md`; `docs/notes/P25/similarity-closest.md` | The `similarity --closest 20 --report` gate fails until a person gives every region a verdict (`independent`, `protocol-mandated`, `convention`, `fix`); a `fix` verdict means a rewrite before the tag |
| RM18 | Acceptance map: 59 rows still `Review:` (counted at `bee44d9`): live vendor lanes (K7–K12, K45, K46, D4-1, D5-2 Apptainer, S1-1, S2-1, S3-3), recorded live captures (A44-1, A45-1, A46-1 incl. the real-key P09 demo), parts owned by later tasks (A17-1, A53-1, A68-1, B9/B10/B12/B16/B26, C7-1, C12-2, E6-1, E18-1, G14-1, G19-1, I4/I6/I7/I51/I54, K28-1/2, K37-1, K38-1, M1-1, S8-1) and items with no task (A22-1, S11-1, S13-1) | `docs/internals/acceptance-map.md`; `docs/notes/P25/acceptance-strict.md`; `HANDOFF.md` owner items | `acceptance --strict` is a release gate and fails on every Review row; each needs a test, a live run by the owner, or a Known-gaps row with its §2.8 D-id |
| RM19 | Traps ledger: 24 rows still `Review:` (R01.1, R02.7–9, R02.13, R02.22–23, R03.28, R05.4–5, R05.11, R05.22, R06.12, R06.22, R07.14, R07.22, R07.30, R08.11, R08.20, R08.22, R09.20, R10.17, R10.19, R10.20) | `docs/internals/traps-ledger.md`; `docs/notes/P25/traps-complete.md` | `traps --strict` is a release gate; each row needs a test or the lead's signed accepted review (R07.30 is the proposed candidate) |
| RM20 | Parity: 16 obligations open — the reference captures for the ten runnable flows are owed (owner, REA sandbox), then compare and classify every divergence | `docs/internals/parity-report-v1.md`; `docs/notes/P25/parity-compare.md` | "Done when" needs a ledger with no open or failed obligation and no unclassified divergence; the parity gate needs `PARITY_REF` |
| RM22 | The Nix lane has never run: `flake.lock` is not committed and `nix/pins.json` `pnpmDepsHash` is null (checked) | `docs/notes/P23/nix-flake.md`, `nix-modules.md`; LEAD-NOTES "P23 wave 36" | The lane is in the required CI gate and in the release workflow; uv2nix / pnpm API names are unconfirmed, so its first run decides whether main stays green |
| RM23 | The Docker workflow has never run: image builds, image tests, hadolint and actionlint, the image sweep; the base digest was resolved by hand | LEAD-NOTES "P23 wave 36" (follow-ups, done-when); `.github/workflows/docker.yml` | The release publishes exactly the archives the docker lane tested; an untested lane blocks the release workflow at its gate |
| RM24 | Windows and macOS lanes never run green: `install.ps1` behaviour tests (pwsh absent here), the Windows install e2e, the desktop packaged builds and Electron suites on macOS and Windows, the Electron security-audit spec (wave 43) | `docs/notes/P23/` (install-ps1 notes); `docs/notes/P20/e2e-demo4.md`; LEAD-NOTES "P25 wave 43" (Gate, P25-03) | 1.0 supports three OSes; a Windows or macOS installer that has never run in CI cannot be released |
| RM25 | Performance baseline not recorded on the CI runner (`tooling/perf/baseline.json` empty, checked); the desktop cold-start budget unmeasured; `oneshot_first_token` has about 30 ms of headroom (1471 ms against 1500). Wave 48 (builders e, g; D-16.1084 – D-16.1086, D-16.1090 – D-16.1092) cut start-up cost: on the integrator's box the one-shot workload's median fell from 2055 ms (main) to 1658 ms (integrated tree; alternating, 9 samples each), but `pytest -m perf` still measures 1763 ms against the unchanged 1500 ms budget there; next cuts in the perf note (config defaults twin, skill front-matter twin) | `docs/notes/P25/perf-python.md`, `perf-ts.md`; LEAD-NOTES wave 42 | Budgets must be judged on clean runners; without a baseline a regression over 10 % is not detected, and the one-shot budget may fail on a slower runner |
| RM26 | Release contributor map: the release dry run exits 1 on unmapped authors, one of them the hash of an empty author email (`contributors/` holds only its README, checked) | LEAD-NOTES "P23 wave 36" (release dry run) | `release.py` refuses a stable cut until every author since the last release is mapped; the owner maps the authors or fixes the commit metadata |
| RM27 | The Compare step was never walked for three P24 tasks: memory caps, plugin packs, egress proxy (unticked checklists) | `docs/notes/P24/memory-caps.md`, `plugin-packs.md`, `egress-proxy.md` | Definition of done item 2 (AGENTS.md): code written without the compare walk may miss a reference branch; the lead walks it and closes gaps |


### Should-fix (14)

| # | Item | Source | Why |
| --- | --- | --- | --- |
| RS1 | Wave 43 defence-in-depth follow-ups: the dashboard's inline bootstrap script blocks a hash-based script CSP; `cookies.read` accepts the unprefixed cookie name on https; `IpcRegistry.seal()` does not sender-check unregistered sync channels; gated mode lets a request without `Origin` past the CSRF check | threat model SEC-18; LEAD-NOTES "P25 wave 43" follow-ups | Each is limited by another control (SameSite=Lax, the IPC allow-list, the session credential) but weakens a layer of the web and desktop hardening |
| RS2 | The desktop's local attach cannot check the control socket listener's uid (Node has no `SO_PEERCRED`); the Python client does (`listener_is_ours`, checked) | `docs/notes/P20/local-attach.md` | Another local user who wins the socket path could answer the desktop; the 0600 socket and private pointer folder keep the window small |
| RS5 | `LspUpkeep.release` is not called before a worktree is removed; `indus lsp restart` cannot reach a running daemon | `docs/notes/P24/lsp-client.md`; LEAD-NOTES wave 42 follow-ups | A language server keeps files of a removed worktree open (removal fails on Windows); restart needs a daemon restart |
| RS8 | GitHub Actions still pinned by tag: `ci.yml` (checkout@v4, setup-uv@v6, cache@v4, …), `docs.yml` Pages actions, `docker.yml` v4 actions (checked) | `docs/notes/P23/tool-versions.md`; `docs/notes/P25/docs-site.md` | A moved tag can change what runs in CI and in the Pages deployment; the release workflow is already SHA-pinned |
| RS12 | Messaging owner decisions with safe defaults in force: the WhatsApp provider (the bridge ships without a WhatsApp Web library, D-16.531), Photon ships disabled (D-16.564), Matrix E2EE on libolm (deprecated upstream, optional Linux extra) | `HANDOFF.md` owner decisions (1), (4) | The features ship limited until the owner decides; nothing unsafe ships meanwhile |
| RS13 | `indus pm` owner decisions: ffmpeg origin (D-16.801), Lightpanda and agent-browser (D-16.802) | `docs/plan/16-what-we-change.md` §3.2 | Both stay off the managed catalogue (found on PATH) until decided |
| RS14 | Running a paused cron job by hand runs it without resuming the schedule: the owner confirms that this is intended | `HANDOFF.md` owner items | If not intended, users get a different schedule state than they expect |
| RS15 | The `bootstrap-mirror` release of the release base has no assets: the pinned uv and PortableGit files must be uploaded by the owner | LEAD-NOTES "P23 wave 36" owner items | The installers fall back to the mirror after network errors; an empty mirror turns a retryable failure into a hard one |
| RS17 | Housekeeping on the build machine (agents may not delete): `/root/indus-backup-2026-01-01-090000.zip`, log lines under `/root/.indus`, one pnpm cache line, npm debug logs, an earlier uv cache slip, `/tmp/pytest-of-root` (about 2 GB), `/root` (about 6 GB) | `HANDOFF.md` owner decisions (5), "Watch out for" | No user impact; disk on the build box fills up (ENOSPC stopped a wave on 2026-10-21) |
| RS18 | Manual terminal checks never ticked: kitty, iTerm2 / WezTerm, Windows Terminal (WSL2) for key reporting, flicker and resize | `docs/notes/P12/ink-spike.md` | Key handling and drawing on real terminals are unverified; CI uses a PTY only |
| RS20 | The skills hub's skills.sh search answer shape was taken from observed behaviour, never checked against published docs | `docs/notes/P13/hub-sources.md` ("verify before 1.0") | Hub search may fail against the live service; a one-time online check |
| RS21 | Desktop end-to-end gaps: clarify cards and the Deny path end to end; quit mid-turn with a running tool child under a process census; steer, queued input, session switch and WebSocket drop | `docs/notes/P20/e2e-demo4.md` | These flows are covered by unit tests only; a regression would reach users unnoticed |
| RS22 | Live and long runs only the owner can do: vendor smoke tests per messaging platform, the two-hour live session, a real Apptainer run | `HANDOFF.md` owner follow-ups (P21/P22); `docs/notes/P08/long-session.md` | Real services and long sessions find what fakes cannot; the acceptance rows they close are in RM18 |
| RS37 | pypdf 6.19.0 has four moderate advisories and one low; the fixed 6.20.1 (all four moderates) passes the 14-day `exclude-newer` rule at lock date 2026-10-27, 6.21.1 (the low one) at 2026-11-01 (W48-f-3) | `docs/notes/P25/deps-audit.md`; D-16.1089; `tooling.checks deps_audit` (warns) | A document-reading dependency with known advisories ships unless the lock moves on or after 2026-10-27 (re-lock `uv.lock`, refresh the audit snapshot and notices) |


### Later (26)

| # | Item | Source | Why |
| --- | --- | --- | --- |
| RL1 | Non-English catalogues and a docs translation (inventory Q9) | acceptance map Known gaps; D-16.51 | Owner settled English only for 1.0; the catalogue mechanism and key-parity check ship |
| RL2 | The Teams meeting pipeline and the Home Assistant adapter (inventory K12) | acceptance map Known gaps; D-16.87, D-16.86 | Separate media pipeline and vendor permissions; Home Assistant fits the plugin API |
| RL3 | MCP catalogue install from `indus mcp` (inventory G19) | acceptance map Known gaps; D-16.45 | A catalogue is a review and hosting process, not code |
| RL4 | Import from another agent product's home (inventory I46) | acceptance map Known gaps; D-16.50 | Low value for a new product |
| RL5 | Telemetry and install-receipt upload (inventory I48) | acceptance map Known gaps; D-16.49 | Privacy default: receipts stay local |
| RL6 | The light, remote-only desktop variant (inventory N30) | acceptance map Known gaps; D-16.48, D-16.15 | Packaging matrix cost |
| RL7 | A separate native setup installer app (inventory N33) | acceptance map Known gaps; D-16.47 | The desktop's in-app installer covers first run |
| RL8 | The Termux APT channel (inventory S5; P24-21) | acceptance map Known gaps; D-16.15 | No capacity in P24; post-1.0 |
| RL9 | MSIX and Store packages (inventory S6; P24-22) | acceptance map Known gaps; D-16.15, D-16.70 | Signed NSIS ships at 1.0 |
| RL10 | The legacy single-query runner script (inventory O9) | acceptance map Known gaps; D-16.85 | `indus -z` and `indus chat -q` cover one-shot runs |
| RL11 | The native-app OAuth broker (RFC 8252, `/auth/native/*`) | D-16.875; LEAD-NOTES wave 42 follow-ups | A security surface of its own; the desktop keeps the token-mode fallback (D-16.589) |
| RL12 | Make placeholder traps Review rows fail the everyday check, not only `--strict` | `docs/notes/P25/traps-complete.md`; LEAD-NOTES "P25 wave 41" follow-ups | A lead policy choice; `--strict` already gates the release |
| RL13 | Reference media hashes for Docker images (a lane that may fetch the reference) | LEAD-NOTES "P25 wave 41" (CI wiring, follow-ups) | Images are swept for names and hosts already; hashes need the reference outside docker files |
| RL14 | Messaging adapter extras (not re-checked): Slack native slash commands; ntfy inbound attachments; Buzz cursor persistence, edits, reactions and files; Signal native text styles; IRC services-account identity and `#` chat routing; Telegram `/topic` command (D-16.570); WeCom authenticated socket; Raft sidecar and hook wiring; WhatsApp Cloud markup in core; a Google Chat base-class seam; Email `unknown_dm_mode` and long text as an attachment; Matrix cross-signing and sends through the daemon; Discord voice extras; Teams small follow-up | `docs/notes/P15/slack-adapter.md`, `telegram.md`; `docs/notes/P16/` (ntfy, buzz, signal, irc, telegram-topics, wecom, raft, whatsapp-cloud, whatsapp-bridge, google-chat, email, matrix-e2ee, discord-voice, targets, teams) | Platform polish beyond the 1.0 contract suite; each adapter passes its contract |
| RL15 | Media and housekeeping (not re-checked): Docker container-path translation for media and cache paths; pruning of cached media by age; a sweep of code-kernel staging folders left by a host that died | `docs/notes/P15/media-policy.md`, `intake.md`; `docs/notes/P10/media-generate.md`, `code-kernel.md` | Disk use grows slowly and container paths need a host path; no data loss |
| RL16 | Agent loop and providers (not re-checked): `invalid_tool` strikes walk the fallback chain; a `model_entitlement` classifier reason; pricing rows for DeepSeek peak/off-peak, xAI, Mistral, Bedrock regions; fast mode stored per session; a sleep-aware idle meter; approval observer hooks around the smart judge | `docs/notes/P08/fallback-chain.md`, `pool-runtime.md`, `usage-cost.md`, `fast-mode.md`, `liveness.md`, `smart-approvals.md` | Refinements; current behaviour is safe (turn ends, cost unknown rather than wrong) |
| RL17 | TUI refinements (not re-checked): model picker bridge (`ProviderOption.models`, a refresh flag), config sync sending null for unset keys and the missing keys, the live cap cutting inside a code fence, the idle-gap key re-assert, editor hand-off, stdin drain, attach-specific restart wording | `docs/notes/P12/model-picker.md`, `config-sync.md`, `markdown.md`, `ink-spike.md`, `app-bootstrap-recovery.md`, `composer-extras.md`; `docs/notes/P17/tui-attach.md` | Cosmetic or rare; workarounds exist |
| RL18 | Desktop refinements (not re-checked): per-profile connections in main; a `customWindowControls` fact for WSLg; PowerShell discovery beyond PATH; per-profile model status; a durable queue (`admission.hold`); the TUI colour payload and terminal font from themes; full-screen detection for the pet on Linux and Windows; ReadAloud for voice; SSH remote pane calls; capabilities page backend; a QR code for pairing; cron run history and per-child subagent events; Windows keep-data uninstall with a system Python | `docs/notes/P20/profile-routing.md`, `preload-bridge.md`, `installer-runner.md`, `model-status.md`, `busy-actions.md`; `docs/notes/P24/themes.md`, `pet.md`, `desktop-voice.md`, `ssh-remotes.md`, `capabilities-page.md`, `management-pages-1.md`, `management-pages-2.md`, `desktop-uninstall.md` | Enhancements of shipped features |
| RL19 | Dashboard and web refinements (not re-checked): profile operations in `service` instead of a child process (D-16.620); the update check helper in `foundation` (D-16.622); a daemon-side fenced delete verb; workspace options in the chat rail; MCP OAuth `redirect_login` (D-16.617); the plugin-route socket gate; the TUI sidecar credential; PTY lineage canonicalisation and a spawn helper; cleanup of chat uploads (D-16.654) | `docs/notes/P18/` (rest-profiles-system, rest-sessions, chat-rail, rest-cron-skills-mcp, chat-ws-routes, pty-registry, pty-bridge); `docs/notes/P24/dashboard-plugins.md` | Structure and tidiness; behaviour is correct today |
| RL20 | Daemon and protocol refinements (not re-checked): an `admission.state` emitter and admission-to-turn matching by id; events for compaction child ids; the person's typed text shown on other surfaces; the macOS suspend clock; `--local` onto the durable lease; a daemon event per subagent child turn | `docs/notes/P17/protocol-attach.md`, `remote-host.md`, `turn-liveness.md`, `cli-attach.md`, `shared-backend-demo.md`; `docs/notes/P18/e2e-m6.md`; `docs/notes/P24/management-pages-2.md` | Multi-surface polish; single-surface use is unaffected |
| RL21 | Board, verify, pm and LSP refinements (not re-checked): the board worker writes its exit code beside its log; a saved verify recipe (`.indus/verify.json`, `--save`); cross-target staging and re-install handling in `indus pm`; `lsp.exclude_roots`; a vendored A2A schema fixture | `docs/notes/P24/board-dispatcher.md`, `verify.md`, `pm-commands.md`, `pm-core.md`, `lsp-client.md`, `a2a.md` | Enhancements; a crashed worker is booked as a crash meanwhile |
| RL22 | Skills, plugins, MCP and cron refinements (not re-checked): rename recovery for bundled skills; refusing a curator-fork delete; bundle names in a cron job's skill list; the cron toolset mapping; gateway buttons for hub consent; background supervision for plugins; the MCP deferral flag and `--toolsets` wiring in the CLI host; MCP OAuth snapshot and restore; media path trust in MCP calls | `docs/notes/P09/bundled-sync.md`, `skill-manage.md`; `docs/notes/P14/cron-prompt.md`, `cron-runner.md`; `docs/notes/P13/hub-cli.md`, `plugin-context.md`, `mcp-discovery.md`, `mcp-oauth.md`, `mcp-calls.md` | Edge cases with workarounds (CLI consent, explicit skill names) |
| RL23 | Foundation and test refinements (not re-checked): clear credential-shaped variables and pin `TZ=UTC` in the autouse fixture; fold the per-file `drive` / `pump` helpers into one; group-readable logs for managed installs and a component filter for `indus logs`; dispatcher `read_tracking` calls; the process-receipt lineage resolver; an async-fed Docker probe | `docs/notes/P00/fakes-and-clock.md`; `docs/notes/testing/drive-holds-clock.md`; `docs/notes/P01/logging.md`; `docs/notes/P05/read-file.md`; `docs/notes/P11/process-receipts.md`, `docker-environment.md` | Test hygiene and small capabilities; no user-facing defect |
| RL24 | Packaging refinements (not re-checked): `update.log` rotation; the slim sandbox image size; the uv and Git for Windows bump (2.56 exists); web and TUI pages from a release payload in `install.sh`; a root-drop shim for `docker exec` | LEAD-NOTES "P23 wave 36" follow-ups; `docs/notes/P23/install-sh-runtime.md`, `docker-entry.md` | Size and convenience; current pins are valid and verified |
| RL25 | The subprocess "external agent" provider transport and dashboard plugins beyond the SDK basics | `docs/plan/18-phase-wise-build-plan.md` §4; `16-what-we-change.md` §2.4 | Owner's call after 1.0 |

| RL26 | Native Windows (P25-24 follow-ups): background processes, the `indus verify` host, cron scripts and the code-execution launcher are still POSIX-only on native Windows (each needs a Windows branch or a "use WSL2" refusal); the `windows-local` CI lane is advisory until its first green run. Partly done in wave 47 (builder e; D-16.1043 – D-16.1047): `indus verify`, cron scripts and the code launcher run through Git Bash in a job object, host background processes and the kernel's `pass_fds` refuse with the WSL2 message; tested on Linux with the platform patched only. Wave 48 (builder d; D-16.1082, D-16.1083): the `terminal` tool no longer offers `background` / `pty` / `notify` / `heartbeat` / `persist_on_release` with a host-local backend on native Windows, and the admin console's runner (`service/console.py`) uses a job object. Left: the `platforms("windows")` real-child cases on the `windows-local` lane, a native background-process port if wanted, `plan()` still turning a trailing `&` or a timeout over 600 s into a background run on a Windows host | `docs/notes/P25/windows-local.md` | Native Windows is best effort for 1.0 (D-16.55); WSL2 is the supported Windows path |

### Done in this wave (32)

Closed after the list was written, each checked in the code before it moved here. The lead fills in
the commit of each row.

| # | Item | What closed it | Commit |
| --- | --- | --- | --- |
| RM1 | Land wave 43 (P25-01..04): the threat model, the `security_static` guard, `tests/security/`, the desktop and web security suites, the dependency audit and their fixes SEC-1, SEC-2, SEC-3, SEC-11, SEC2-1 (= SEC-16); re-check the threat-model rows that cite wave-42 surfaces | Landed (D-16.900 – D-16.912): checked on main `7d9a864` — `tools/builtin/extract.py` has the zip budget (`ZIP_MAX_MEMBERS`, `ZIP_MAX_UNPACKED_BYTES`), `core/untrusted.py` lists `a2a_call`; the wave-42 rows re-checked (D-16.912; SEC-12, SEC-13 fixed at that landing). Still open from wave 43: SEC-4 (RM2), SEC-6/8 (RM4), SEC-7/10 (RM5), SEC-9 (RM6), SEC-17 (RM7, RM8), SEC-14 (RM28), SEC-15 (RM29), SEC-18 (RS1) | `4945b48` |
| RM3 | SEC-5: `message_agent` answers from peer gateways reached the model unfenced | `message_agent` joined `core.untrusted.UNTRUSTED_NAMES` at the wave-43 landing (D-16.909; every reply fenced, local profiles too); checked on main `7d9a864` | `4945b48` |
| RM30 | F-1: the release workflow attached only the desktop artefacts, so `install.sh`, `install.ps1` and `install.cmd` were never on a release while the install guide and `install.cmd` download them from `releases/latest/download/…` | `release.yml` `upload` job takes the three installers from the admitted commit (sparse checkout) into the uploaded artefacts and the manifest (D-16.960); pinned by `tests/unit/ci/test_release_workflow.py::test_upload_attaches_the_installers_from_the_admitted_commit` | wave 44 landing (this integration) |
| RM2 | SEC-4: fence laundering — a script run by `execute_code` can fetch a page over RPC and print it, and `delegate_task` returns a summary of fenced text, both unfenced | Wave 46 (builder a; D-16.990 – D-16.993): taint, not per-RPC fencing — `core.untrusted.Taint` marks an `execute_code` run that called an untrusted tool (sticky per kernel owner until `reset`) and a `delegate_task` whose child read outside text; the tool round fences a tainted result whatever its tool's name, background deliveries are fenced. Threat model SEC-4 fixed (B-INJECT-1); residuals in its row (a file written then read back, model-copied text) | `44c9a4f` |
| RM4 | SEC-6 (`str.isdigit()` then `int()` on outside text in about 30 adapter and route spots) and SEC-8 (spawns that inherit the process environment: the dashboard's git update, doctor probes, the editor) | Wave 46 (builder c; D-16.997 – D-16.999): `foundation.digits` (`is_digits` / `parse_digits`, ASCII only, capped) at 62 spots in 45 files (SEC-6); the dashboard's git update, doctor and locate probes spawn with explicit child envs and three `inherit_env` allow entries are gone (SEC-8). Left for the owner: written acceptance of the editor residual (D-16.998) and of `GH_TOKEN` / `GITHUB_TOKEN` no longer reaching the dashboard's git update (D-16.999) | `44c9a4f` |
| RM8 | Re-lock aiohttp 3.14.3 → 3.14.4 (SEC2-3, slack extra), vite 8.3.2 → 8.3.3 (SEC2-4, dev), pynacl 1.5.0 → 1.6.2 (lockable now) | Wave 46 re-lock (builder b; D-16.994 – D-16.996): aiohttp 3.14.4, vite 8.3.3, pynacl 1.6.2 (override over discord.py's `<1.6` cap); `exclude-newer` 2026-10-08, `pins_meta.json` lock date 2026-10-22; the aiohttp and vite `deps_audit` allow entries dropped; notices rows re-keyed. certifi is RM7 (owner) and stays allow-listed to 2026-11-08 | `44c9a4f` |
| RM21 | Nine desktop `data-ui` hooks owed (`OWED_HOOKS`: onboarding dialog and its five steps, transcript tool row, the files and review pane toggles), so the two desktop parity flows cannot run | Wave 46 (builder d; D-16.1000, D-16.1001): `data-ui` hooks on the onboarding dialog and its five steps, the transcript tool row, and new files / review pane toggles; `scripts/parity/scenarios.py` `OWED_HOOKS` is empty. The two desktop flows still run only in the owner's packaged-app lane (RM20) | `44c9a4f` |
| RM28 | SEC-14: one language-server tier per process, so on a multiplex host the servers started for one served profile get the first profile's child env and `lsp.servers` settings | Wave 45 (S45; D-16.970): one tier per served profile, keyed by its home, with that profile's child env and `lsp.servers`; at this integration P45's `lsp-restart` / `lsp-release` reach only the asking profile's tier and a removed subagent worktree's servers stop in every tier (D-16.1021; two-profile test in `tests/unit/service/test_lsp_control.py`) | `44c9a4f` |
| RM29 | SEC-15: a room member's turn prompt lists the other members' replies as unfenced `who: text` lines, so a reply holding a newline can forge a line in the person's name | Wave 45 (S45; D-16.971): `rooms.loop.transcript_entry` fences each member reply (`untrusted.wrap`, any length) under a `name (member):` line, speaker names sanitised; `tests/security/test_trust_boundaries.py` pins that a reply cannot forge a speaker. Room approvals stay RS3 | `44c9a4f` |
| RS4 | The TUI model picker shows "this provider lists no models" for a custom endpoint whose `/v1/models` lists six (setup offers them) | Wave 46 (builder f; D-16.1008): protocol `ProviderOption.models`; `tui_bridge/methods_config.py` lists the current custom or local provider's models live (`model_list.list_models`), the TUI picker shows them | `44c9a4f` |
| RS6 | A daemon killed between a turn's streamed reply and its terminal write shows "a turn was lost" with Discard although the reply was saved | Wave 46 (builder f; D-16.1009): `gateway/daemon/recovery.py` discards a routeless lost turn quietly when the transcript proves the reply was saved (`RecoveryReport.saved_turns`) | `44c9a4f` |
| RS7 | `notices.py refresh` writes `notices_data.json` in a layout Biome rejects, so every refresh needs a manual reformat | Wave 46 (builder g): `scripts/release/notices.py` `dump_json` writes the layout Biome keeps (160 width cases byte-identical to `biome format`; the committed file round-trips unchanged) | `44c9a4f` |
| RS9 | shellcheck does not run in CI for the installer fragments and the release scripts (checked: no step in `ci.yml`) | Wave 46 (builder g; D-16.1012): `ci.yml` job `shell` runs shellcheck 0.9.0 (runner apt archive, version asserted) over the built `install.sh` and `scripts/release/*.sh`; the `gate` job needs it. First CI run pending | `44c9a4f` |
| RS10 | User-guide pages for the P24 features now wired (rooms, board, bots and peers, relay, themes, pets, vault, egress, `indus pm`, LSP, verify) | Wave 46 (builder h; D-16.1014, D-16.1015): user-guide pages board, bots and peers, rooms, themes, pets, vault, egress, helper programs (`indus pm`), LSP, verify and messaging relay, in the mkdocs nav; the P45 paragraphs (`message_agent` in bot chats, `indus lsp restart` asking the gateway) added at this integration | `44c9a4f` |
| RS11 | Tests that bind a Unix socket under `tmp_path` fail with a long base temp path; `test_manual_approval_denied_spawns_nothing` depends on a line wrap | Wave 46 (builder g): `tests/short_paths.py` and the `short_tmp` fixture for every test that binds a Unix socket (watchdogs, rpc, control socket, attach verbs); the runner's line-wrap test no longer depends on the terminal width | `44c9a4f` |
| RS16 | P24 wiring partial (D-16.878): suggestion offers on integration connect, the remaining desktop contribution areas (title bar, palette command, keybind, theme) and the plugins inventory page, `message_agent` offered in bot-chat turns | Wave 45 (P45; D-16.980, D-16.981): `message_agent` in canonical bot-chat turns (platform `bots`), suggestion offers when an integration is connected; wave 46 (builder e; D-16.1004 – D-16.1007): title-bar strip, command palette with palette-command entries, contributed keybinds, plugin themes, the `app.plugins` pane. D-16.878 updated. Follow-ups (not in this row): the composer-extension, session-row, model-menu, appearance-setting and transcript-directive consumers | `44c9a4f` |
| RS19 | `post_tool_call` does not fire for a call that a `before` hook blocked (`dispatch` returns early, checked at `tools/dispatch.py:190`) | Wave 46 (builder g; D-16.1011, D-16.1013): `tools/dispatch.py` runs the after hooks for a blocked call (`BLOCKED`, plugin payload `blocked`, no `transform_tool_result`) | `44c9a4f` |
| RS23 | F-2: the desktop app has no licences screen; `THIRD_PARTY_NOTICES.md` ships inside each package (the smoke checks the file) | Wave 46 (builder d; D-16.1002, D-16.1003): Settings → About → Licences shows the packaged `THIRD_PARTY_NOTICES.md` as plain text (main reads it, IPC `indus:about:notices`, at most 4 MiB) | `44c9a4f` |
| RS24 | F-3: a script install has no built dashboard pages; `indus dashboard` builds them on first launch only when pnpm and Node are on the machine, otherwise only the API answers (the smoke's `dashboard-login` fails) | Wave 46 (builder k; D-16.1016 – D-16.1018, D-16.1020): `python -m indus.web.bundle fetch` (HEAD's release tag → manifest digest → `indus-web-<version>.tar.gz`) from `install.sh`; this integration added the `release.yml` `web-pages` job that builds, sweeps and attaches the tarball (D-16.1022). Follow-ups closed in wave 47 (builder f; D-16.1048 – D-16.1050): `indus update --finish` refreshes the pages (phase `web_pages`), `install.ps1` fetches them like `install.sh` (and writes the marker `version`), and pages stamped for another release are `stale` and not served; `install.ps1` not yet run on Windows (RM24) | `44c9a4f`; follow-ups wave 47 integration |
| RS25 | F-4: a source install's `indus --version` prints `0.0.0` (releases stamp the version into build trees only); the release shows only in `indus update --check` / `--plan` | Wave 46 (builder k; D-16.1019): `cli/version.py` shows the release HEAD is on (tags, installer marker, update receipts; file reads only) | `44c9a4f` |
| RS26 | F-5: with no terminal, `indus chat -q` refuses a risky command when run `--local` (`approvals.single_query_mode`) but, attached to the daemon, leaves it waiting for an approval and exits 3 | Wave 46 (builder f; D-16.1010): attached finite `chat -q` without a terminal denies an approval with the `--local` wording (approves only under `approvals.single_query_mode: approve`); the daemon-side flag stays D-16.487. Superseded in wave 47 (builder g; D-16.1051 – D-16.1053): `chat -q` and `-z` send `prompt.submit.single_query`, the daemon runs the turn single-query and asks nobody, as `--local` does; the client-side answer is removed | `44c9a4f`; wave 47 integration |
| RS27 | Desktop chat: the composer stays on Stop/Steer for minutes after a turn ends although the model is idle (intermittent; a window reload clears it) (screenshot capture, `HANDOFF.md` 05:05 UTC) | Wave 47 (builder a; D-16.1030, D-16.1031): `busyActions.ts` follows the generation (turn ids seen in `turn.started`, the newest ended generation); `sharedActions` subscribes for its lifetime, so sends with no composer mounted see their end | wave 47 integration |
| RS28 | Desktop chat: after a window reload, transcript tool rows lose their path/command summary and the expanded row loses Arguments (screenshot capture) | Wave 47 (builder a; D-16.1032, D-16.1033): `HistoryMessage.arguments` (masked like `tool.started`) from the TUI bridge and REST `/api/sessions/{id}/messages`; desktop `restHistory` and `messageStream` rebuild rows with them | wave 47 integration |
| RS29 | Desktop file tools resolve relative paths against the daemon's work folder, not the chat's folder (the Terminal pane uses the right one) (screenshot capture) | Wave 47 (builder b; D-16.1034 – D-16.1037): `AgentOptions` / `SessionRuntime.workdir`, tools get `with_workdir` on the local backend; attach-again restores a person surface's stored folder; desktop `freshDraftFolder`. Follow-up closed in wave 48 (builder c; D-16.1076): a gone folder gives one `workdir_missing` warning notice per turn | wave 47 integration; follow-up wave 48 integration |
| RS30 | TUI attached to the daemon (the default) gets "… is not served here" for `complete.slash`, `model.options` and `session.list`: the slash menu, Ctrl+O model picker and Ctrl+X sessions picker fail (screenshot capture) | Wave 47 (builder c; D-16.1038 – D-16.1040): the daemon's WS table serves all three from `indus.service.picker_reads` (shared with the TUI bridge; session-scoped calls need an attached session); `config.*` stays -32601 (D-16.549). Wave 48 b serves `config.get` too, so only `config.set` is left (RS33) | wave 47 integration |
| RS31 | Dashboard Chat tab in the light theme: the embedded TUI's prompt and reply text are nearly invisible (screenshot capture) | Wave 47 (builder d; D-16.1041): the dashboard's xterm answers colour-scheme reports from `data-theme` and the TUI follows them (`followTerminalScheme`, DSR 996 / 997, mode 2031, late OSC 11) | wave 47 integration |
| RS32 | Small UI bugs: the TUI slash-menu row leaves stray characters at its end, the desktop slash-menu highlighted description has low contrast, session titles are cut too early ("Chat: How do I") (screenshot capture) | Wave 47 (builder d; D-16.1042): `CompletionList` count line takes a row and truncates; highlighted description uses `text-accent-fg`; `clean_title` keeps the first line up to 60 characters with "…" | wave 47 integration |
| RS3 | Room turns: nobody can answer an approval (the turn waits until stopped or 3 h), and room events are not pushed to surfaces. Partly done in wave 47 (builder h; D-16.1054 – D-16.1056): the daemon half — `approval.requested` / `.answered` / `.settled` in the room log, `groups.approve` (only a person answers), `groups.watch` / `groups.unwatch` pushing `groups.event` over WS. Left: desktop `GroupRoom.tsx` and the TUI / web room views still poll `groups.log` and show no approval card | Daemon half in wave 47 (builder h; D-16.1054, D-16.1055). Wave 48 (builder a; D-16.1070): desktop `GroupRoom.tsx` follows `groups.watch` / `groups.event` (re-watch from the last `seq` on reconnect, polling only when the watch is refused) and answers `approval.requested` with a room approval card (`groups.approve`, the request's own choices, refusals shown); jsdom-tested, not yet seen in a real Electron window. The TUI and the web dashboard have no room view; building one is a new surface, an owner question (D-16.1071), not counted here | wave 48 integration |
| RS33 | TUI attached to the daemon (the default): the sessions picker rows show only the id, where `--local` shows title, model and source (screenshot pass, `HANDOFF.md` minor UI follow-ups) | Wave 48 (builder b; D-16.1072 – D-16.1075): `session.list` rows carry `SessionRow.model`, `sessionSwitcherState` fills live rows from the stored rows; the daemon also serves `complete.path`, `commands.catalog` and `config.get` (only `config.set` stays -32601, D-16.549); wave 48 h serves `slash.exec` / `command.dispatch` and the session header and title events (D-16.1093 – D-16.1098) | wave 48 integration |
| RS34 | Desktop About › Version log shows an ERROR line "ResizeObserver loop completed with undelivered notifications" and long run paths (screenshot pass) | Wave 48 (builder c; D-16.1078, D-16.1079): the renderer drops the benign ResizeObserver notice, `TerminalPane` refits on the next animation frame, and forwarded renderer errors keep only the file name of `file:` URLs | wave 48 integration |
| RS35 | Desktop transcript: an approval card that opens below an expanded tool row is not scrolled into view (screenshot pass) | Wave 48 (builder c; D-16.1077): approval and clarify cards scroll themselves into view once when they open (`useRevealOnOpen`); also in wave 48 c: the optimistic user row binds to the turn's own id (D-16.1080) | wave 48 integration |
| RS36 | Dev-dependency advisories: katex 0.16.47 (GHSA-238p-pmpm-9mq7, under mermaid) and sprintf-js 1.1.3 (GHSA-hp3w-g68c-fv3c, under `@electron/get`'s global-agent); `deps_audit` warned on both | Wave 48 (builder f; D-16.1087, D-16.1088): pnpm overrides `mermaid>katex: 0.19.0` and `"@electron/get>global-agent": 4.1.3` (sprintf-js and roarr leave the tree; every new package MIT / BSD / ISC / CC0 and older than 14 days), audit snapshot refreshed (868 packages, 0 failing), notices regenerated | wave 48 integration |

Waves 45 + 46 (landed together by their integrator, 2026-10-22, in `44c9a4f`) closed the eighteen rows RM2 – RS26 above: S45 (SEC-14, SEC-15, SEC-18), P45 (the
P24 partial items), and wave 46's slices a–h and k.

Wave 47 (landed by its integrator, 2026-10-22, commit "wave 47 integration" until the lead fills it
in) closed the six bugs seen while capturing screenshots, RS27 – RS32 (slices a–d), and the
follow-ups recorded in RS24 (slice f: page refresh on `indus update --finish`, `install.ps1` parity,
stale pages) and RS26 (slice g: the daemon-side single-query flag, D-16.487).

Wave 48 (landed by its integrator, 2026-10-22, commit "wave 48 integration" until the lead fills it
in) closed RS3 (slice a: the desktop room view on the room's event push, with an approval card;
the TUI / web room views are owner question D-16.1071), the screenshot pass's minor UI follow-ups
RS33 – RS35 (slices b, c) and the dev-dependency advisories RS36 (slice f), plus the follow-ups in
RS29 (folder-gone notice, slice c) and RS30 (`config.get`, slice b). It added RS37 (pypdf re-lock
on or after 2026-10-27) to Should-fix and moved RL26 on (slice d; still counted in Later).

Partly done at the wave 47 integration (still counted above, with what is left in the row): RS3
(slice h: the daemon half of room approvals and the room event push; the room views were left,
closed in wave 48) and
RL26 (slice e: Windows branches and WSL2 refusals, not yet run on Windows). Wave 44 itself adds
P25-20 (this file and the `checklist` guard), P25-21 (`scripts/release/release_gates.sh`,
`scripts/release/gates.txt`), P25-22/23 (rehearsal and smoke kit; its findings are RM30 and
RS23–RS26) and P25-24 (native Windows terminal backend, best effort; follow-ups RL26); apart from
RM30 none of them closes a row above.

### Withdrawn (0)

Ids that turned out to be duplicates or not items at all. They stay here so they are never reused.

| # | Item | Why withdrawn |
| --- | --- | --- |

### Checked and already fixed (not counted)

- Deleting a session left gateway routes pointing at it — `state/deletion.py` drops
  `gateway_routes` rows by session id (`P15/session-keys.md`).
- The classic CLI ignored the saved toolset selection — `cli/runtime.py` reads
  `saved_selection(scope, "cli")` (`P07/tools-cmd.md`).
- The vision side-model used a 60 s deadline — `providers/auxiliary.py` gives `vision` 120 s
  (`P10/vision-analyze.md`).
- The hub scanner rated a destructive command inside an inline code span `safe` —
  `safety/content_scan.py` scans code-span contents (`P13/hub-install.md`).
- Browser sub-resource requests skipped the SSRF gate — the driver routes every request and socket
  through `RequestGuard` (`P10/browser-page-tools.md`).
- A resumed session over the daemon opened on an empty transcript — `SessionResumeResult.messages`
  carries the stored conversation (`P18/e2e-m6.md`).
- `gateway_control.query` did not check the listener's uid — `listener_is_ours` (P17-24); only the
  desktop half remains (RS2).
- The Feishu document tools had no toolset rows — `tools/toolsets.py` has the `feishu` toolset
  (`P16/feishu-tools.md`).
- The skill bundle tests were missing — written at the P09 landing (`P09/skill-bundles.md`).
- pyright errors in `tooling/checks` and the licence guard for `package.json` files (P00 partial
  notes) — pyright 0 at `226a5ae`; `tooling/checks/tests/test_licenses_packages.py`.
- The desktop's update feed and build stamp vs the release base — D-16.768, D-16.769.
- A room with live tasks did not count as busy for idle exit — D-16.864.
- D-04.1 criterion (e) on the real host — `tests/integration/core/test_d04_1_sync_caller.py`
  exists (the box in `P04/d-04-1-decision.md` was never ticked).
- The `egress` extra was missing from `pyproject.toml` — present (`P24/egress-proxy.md`).
- The desktop's remote WebSocket dial (D-16.691) — built as recommended.
- The wake word's default engine (D-16.800) — applied (D-16.871).
- Not a product item: the owner's request to update two videos (`HANDOFF.md`, 2026-10-20) is
  outside this repository and not counted.

## After v1.0

From `docs/plan/18-phase-wise-build-plan.md` §4 (and `16-what-we-change.md` §2.8); each item keeps
its D-id, and building one moves its row to §2.7.

- **D-16.41** Reading or importing a reference home: an optional one-way importer, owner's call.
- **D-16.45** A curated plugin catalogue and optional-MCP catalogue (RL3).
- **D-16.46** An out-of-process plugin host.
- **D-16.47** A separate native setup installer app (RL7).
- **D-16.48** A light, remote-only desktop variant (RL6).
- **D-16.49** Telemetry and install-receipt upload, opt-in only if the owner asks (RL5).
- **D-16.50** Import from another agent product's home (RL4).
- **D-16.51** Non-English locales and a docs translation (RL1).
- **D-16.15** Termux APT channel and MSIX/Store packages (RL8, RL9).
- The subprocess "external agent" provider transport, if the owner agrees (RL25).
- Dashboard plugins beyond the SDK basics (RL25).
- **D-16.58** Object storage with channel records for release artefacts beyond GitHub Releases.
- Not planned at all: the vendor portal provider (**D-16.12**), impersonating subscription OAuth
  (**D-16.11**), the managed Modal gateway (**D-16.16**).

Ready for `v1.0.0` at `<sha>`: not yet — written here only after §1 is green on that commit and §2
is done; the open owner items are §0 and the must-fix rows above.
