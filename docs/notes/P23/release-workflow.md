# Task: Release workflow and publication controller (P23-23) Phase: 23 Owner: wave-36 builder U1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `.github/workflows/stable-release.yml`
(job graph: admit, lanes, acceptance join, publish), `.github/workflows/stable-release-publication.yml`,
`.github/workflows/canary-release.yml`, `scripts/releases/lanes.py`, and for the controller order
`scripts/releases/stable.py` (`ensure_final_tag`, `edit_draft_release`, `publish_release_draft`,
`publish_attempt`, `require_gate`) and `scripts/releases/sequencer.py` (`plan`). Research 10 §3.17,
traps R10.18, R10.23, R10.24. Closed before writing. Evidence: none.

What it does (own words): `release.yml` runs only by dispatch, from the default branch, with the
attempt ref as input. `admit` proves the run: it is a `workflow_dispatch` on the default branch, the
ref is an annotated tag object on the remote, its peeled commit is on main, its claim message
matches its name and its tagger time; it publishes the commit, version, lanes and a per-job run map.
Every job that builds or tests checks out the admitted commit (never `github.sha`, which is main's
tip): `ci` (full suite + guards), `docker` (the image workflow, pushed by digest and tagged with the
attempt ref), `nix`, `install-e2e` (sh on Ubuntu and macOS, ps1 on Windows), `desktop-mac`,
`desktop-win`, `desktop-linux`, `upgrade` (previous release → this commit through `indus update`).
`gate` is strict: a job whose lane is on must be `success`; `skipped` only counts when its lane was
off in the claim. `upload` sends the desktop artefacts and one `manifest.json` (name → SHA-256) to
the draft once; nothing is rebuilt for publication. `publish.py publish` is the controller: it lists
claims oldest-first and, for each green one that may go out, (1) creates or verifies the annotated
final tag `v<version>` binding attempt ref, attempt object, commit, manifest digest and image
digests; (2) moves the GHCR aliases (`stable` + `latest`, or `canary`) to the receipt's digests;
(3) updates the channel record (P23-21); (4) retargets the draft onto the final tag, strips the
warning blocks and makes it public — last. Each step checks the current state first, so a rerun
after a failure finishes without duplicating anything. `canary.yml` runs daily (and by dispatch) on
the default branch with `cancel-in-progress: false`; when main has no commit newer than the last
canary it does nothing.

Inputs → outputs: attempt ref, GitHub/GHCR clients, `toJSON(needs)` → admit outputs, gate verdict,
receipt tag, aliases, channel record, public release.

Files: `.github/workflows/release.yml`, `.github/workflows/canary.yml`, `scripts/release/publish.py`,
`tests/unit/release/test_publish.py`, `tests/unit/ci/test_release_workflow.py`,
`tests/fakes/release/fake_registry.py`.

Edge cases / failure modes:
- the attempt ref is lightweight, moved, or its message names another commit → admit fails;
- an older claim still running → nothing newer is published;
- an older green claim held (no autopublish) is flushed when a newer claim is green;
- an abandoned or failed claim is skipped;
- a final tag that exists with other content → stop (never overwrite a receipt);
- a draft already public → its steps are verified, not redone;
- the channel asset changed underneath → stop and rerun.

Our design differences / decisions:
- the order is receipt → aliases → channel record → public, so the release becomes visible only
  when everything it points at is in place (the plan's order; the reference publishes first and
  promotes after);
- R2, APT, MSIX/Store, App Installer and downloads-page legs are absent (D-16.58, D-16.15);
- the docker lane calls the image workflow (`docker.yml`, P23-12) with `commit` and `release-ref`
  inputs; the controller reads the pushed digests back from GHCR by the attempt-ref tags
  (interface assumed, see Decisions);
- third-party actions are pinned by full SHA (versions recorded below).

Tests we will write: fake GitHub/GHCR: a rerun after a failure at "move aliases" completes without
duplicating tags; the release is made public after the aliases; an older green claim is published
before a newer one; workflow test: every build/test job uses the admitted commit, the gate treats
skipped selected jobs as failed, dispatch-only, canary never cancels in progress.

Pinned actions (record for `tool-versions.md`): `actions/checkout` v7.0.1
`f548e57e544e1ff5a4c46bf1e1b8685f8e4a348a`; `astral-sh/setup-uv` 8.2.0
`fac544c07dec837d0ccb6301d7b5580bf5edae39`; `cachix/install-nix-action` v31.11.0
`630ae543ea3a38a9a4166f03376c02c50f408342`; `actions/setup-node` 7.0.0
`820762786026740c76f36085b0efc47a31fe5020`; `actions/upload-artifact` v7.0.1
`043fb46d1a93c77aae656e7c1c64a875d1fc6a0a`; `actions/download-artifact` v8.0.1
`3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c`. Not verified against the network from this sandbox:
CI's actionlint lane must resolve them.

## Decisions for the lead
- Interface assumed from P23-12: `docker.yml` has `on.workflow_call` with inputs `commit` (40-hex)
  and `release-ref` (the attempt ref); for a release it pushes, by digest, the four images of
  `scripts/release/claims.py` `IMAGES` tagged `<attempt ref><suffix>`: `ghcr.io/<repo>:rc.N-vX`,
  `:rc.N-vX-browser`, `ghcr.io/<repo>-sandbox:rc.N-vX`, `:rc.N-vX-browser`. Aliases (`stable`,
  `latest`, `canary`, each with the same `-browser` suffix) move only in `publish.py`.
- `release.yml` uses a protected `release` environment for the signing and publish jobs; the
  owner creates it (required reviewers optional) and the secrets named there: `APPLE_ID`,
  `APPLE_TEAM_ID`, `APPLE_APP_SPECIFIC_PASSWORD`, `MAC_CERTIFICATE`, `MAC_CERTIFICATE_PASSWORD`,
  `WIN_CERTIFICATE`, `WIN_CERTIFICATE_PASSWORD` (names only).
- A failed attempt stays open until the owner runs `release.py abandon` (no automatic burn after
  N reruns); the controller skips it, `release.py` refuses to cut past it.
- Desktop lanes call `pnpm --filter @indus/desktop run dist:<mac|win|linux>` (P20-38 / P23-15/16
  scripts, not yet landed) and upload `apps/desktop/release/*`.

Compare checklist (filled after the rewrite; reopened the three workflows, `lanes.py` and the
controller functions):
- [x] dispatch from the default branch only; run name names the attempt
  (test: `tests/unit/ci/test_release_workflow.py::test_release_is_dispatch_only_from_the_default_branch`)
- [x] every build/test job gets the admitted commit (`::test_every_build_job_uses_the_admitted_commit`)
- [x] strict gate (`tests/unit/release/test_publish.py::test_gate_counts_a_skipped_selected_job_as_failed`)
- [x] idempotent controller (`::test_rerun_after_alias_failure_completes_without_duplicates`)
- [x] public last (`::test_release_is_made_public_after_the_aliases`)
- [x] oldest first; running older claim blocks (`::test_older_green_claim_is_published_first`,
  `::test_running_older_claim_blocks_newer`)
- Deliberate differences: no failed-job auto-rerun workflow (rerun the run by hand; the controller
  is idempotent); no one-hour claim grace window — an attempt with no run is "running" until
  abandoned; `abandon` does not cancel a run still in progress (cancel it in the Actions tab);
  the publication order is receipt → aliases → record → public (the reference makes the release
  public before promoting); a canary of a commit whose attempt is still in flight is a no-op
  (`--if-new`), not a resume.
- [x] the receipt is never replaced (`::test_receipt_with_other_content_is_never_replaced`); a
  claim without the docker lane binds no images (`::test_claim_without_docker_binds_no_images`)
- Not taken: Store submission checks, macOS/Windows feed promotion, R2 heads.

## Status

Done (2026-10-21). Nothing changed outside the task's files.

## Integration (wave 36)

Integration (D-16.756, D-16.766): the docker lane calls `docker.yml` with `commit`, `attempt_ref`, `phase: test`; after the gate `docker-publish` (`phase: publish`) pushes the tested archives and `publish` needs it (or its skip when the lane is off). The sandbox images' suffix is `-slim` / `-browser` to match the tags `docker.yml` pushes (`claims.IMAGES`). Desktop lanes run P20-38's `dist:unsigned` into `apps/desktop/build/release` without secrets until P23-15/16. `actions/checkout` v7.0.1 corrected to `3d3c42e…` (the written SHA was not that tag); the other pins verified. The nix lane also runs `nix run .#default -- --version`.
