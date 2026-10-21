# Task: Docker publishing workflow (P23-12) Phase: 23 Owner: wave-36 builder K1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `.github/workflows/docker.yml` (triggers,
matrix and tag policy only). Closed before writing.
Evidence: none.

What it does (own words): builds and tests every image on every pull request, publishes rolling
`main` tags from `main`, and for a release builds once, tests, and pushes those tested bytes by digest
under the attempt ref — never moving a channel alias (R10.23).

- Triggers: `pull_request`, `push` to `main`, `workflow_call` (from the release workflow, P23-23) with
  inputs `commit` (the admitted commit, required for release runs), `attempt_ref` (`rc.<N>-vX.Y.Z`)
  and `phase` (`test` | `publish`); outputs the manifest digests.
- Every job checks out `inputs.commit || github.sha` explicitly (R10.24: the workflow's SHA is not
  the commit under test).
- Matrix: image {product, sandbox} × variant {slim, browser} × arch {amd64 on `ubuntu-24.04`, arm64
  on `ubuntu-24.04-arm`}; native runners, no emulation.
- `lint`: `pins.py check --strict`, hadolint (pinned binary verified by SHA-256 through `pins.py`),
  actionlint likewise.
- `build`: buildx to an OCI archive (`.tar` + SHA-256 file), loaded into the daemon as `:test`, then
  `pytest -m docker tests/docker` against it; release runs upload the archive as an artifact.
- `publish-main` (push to main only): push by digest, then one multi-arch manifest per image/variant
  tagged `main` / `main-browser` (sandbox: `main-slim` / `main-browser`).
- `publish-release` (`phase: publish`): downloads the tested archive (checks its SHA-256), pushes it by
  digest with `skopeo copy --preserve-digests` (nothing rebuilt), creates the multi-arch manifest
  tagged with the attempt ref, and outputs its digest for the publication controller.
- `latest`, `stable`, `canary`, and the sandbox `slim`/`browser` aliases are never written here.
- Every `uses:` pinned to a full 40-hex commit SHA; `permissions` least-privilege (`packages: write`
  only in publish jobs); concurrency per ref, release runs never cancelled.

Files: `.github/workflows/docker.yml`, `tests/unit/ci/test_docker_workflow.py`.

Edge cases / failure modes:
- a fork PR has no package token: PR jobs never log in;
- a release `publish` without the tested archive → fails (never rebuilds);
- an archive whose SHA-256 changed between phases → fails.

Our design differences / decisions:
- plain `docker buildx` / `skopeo` / `docker manifest` commands instead of the docker/* actions:
  fewer third-party SHAs to pin and audit;
- uv for the test job comes from `scripts/docker/pins.py fetch uv` (the installer's pinned uv),
  not a setup action.

Tests we will write:
- the YAML parses; every `uses:` is `owner/repo@<40 hex>`; no step text or tag mentions `latest`,
  `stable` or `canary` as a tag, nor the sandbox aliases; every checkout step uses
  `inputs.commit || github.sha`; release jobs require `commit`; publish jobs alone have
  `packages: write`; the matrix covers both arches, variants and images; release publish downloads
  instead of building.

## Decisions for the lead
- Action SHAs (`actions/checkout`, `actions/upload-artifact`, `actions/download-artifact`) were
  written from memory without network: verify them (`gh api repos/<owner>/<repo>/git/ref/tags/<tag>`)
  before the first run; actionlint does not check SHAs.
- hadolint/actionlint binaries are pinned in `docker/pins.json` (`tools`), digests empty until
  `pins.py refresh`.

Compare checklist (filled after the rewrite; reopened `.github/workflows/docker.yml`, triggers, matrix and tag policy):
- [x] PR build+test, main publishes `main` tags only, release phases test/publish with the attempt ref (`tests/unit/ci/test_docker_workflow.py::test_triggers_and_release_inputs`, `::test_release_publish_skips_build_and_lint`)
- [x] admitted commit threaded to every checkout (`::test_every_checkout_takes_the_commit_under_test`, `::test_release_inputs_are_validated_before_building`)
- [x] aliases never moved here; publish pushes the tested archive (`::test_no_job_moves_a_channel_alias`, `::test_publishing_never_rebuilds`)
- [x] native arm runners, both variants, both images (`::test_matrix_covers_every_image_variant_and_native_arch`)
- [x] every action pinned by SHA (`::test_every_action_is_pinned_to_a_full_commit_sha`); least privilege (`::test_only_publishing_jobs_may_write_packages`)
- Deliberate differences: plain docker/skopeo commands instead of the docker/* actions; uv from our pins;
  the sandbox image published by the same workflow; no separate lint workflow (the lint job is here).

## Status

Done (2026-10-21) as a static artefact: the workflow never runs here. Owed before its first run: verify the
three action SHAs, run `scripts/docker/pins.py refresh`, and confirm the GHCR namespace (owner decision).
Files: `.github/workflows/docker.yml`, `tests/unit/ci/test_docker_workflow.py`.

## Integration (wave 36)

Integration: the three action SHAs were verified against their tags (`git ls-remote`). `release.yml` now calls this workflow with exactly `commit`, `attempt_ref`, `phase` — `test` as its gated `docker` lane and `publish` as `docker-publish` after the gate (D-16.756).
