Wave 19 builds two phases that depend only on landed work, from main 730cf11 (P00–P14 done; P15 gateway
wave 1 is being integrated in parallel — do not touch src/indus/gateway/ or src/indus/cron/).
- P21 remaining backends (docs/plan/phases/P21-remaining-backends.md; depends on P11's Docker/SSH
  backends, the backend registry, the execution-contract suite and fake docker/ssh clients).
- P22 research tooling (docs/plan/phases/P22-research-tooling.md; depends on P10 tools, delegation,
  toolsets; trajectories come from the P02 store).
Read your phase file in full (rows, files, fixtures, traps, acceptance), docs/plan/phases/LEAD-NOTES.md
(P05, P11, P10, P13 landing notes and the gate change) and HANDOFF.md.
Slices (each owns only its tasks' files):
- B1: P21-01 sandbox records store, P21-02 SDK call wrapper, P21-03 payload stdin staging, P21-04
  Singularity/Apptainer backend, P21-11 vendor credentials and env hygiene, P21-15 fake SDKs and fake
  CLI. Write the wrapper/staging/records API and the fake SDK surfaces into your notes first — B2
  builds on them in parallel.
- B2: P21-05 Modal direct create/lifecycle, P21-06 Modal exec and file transfer, P21-07 Daytona,
  P21-08 Vercel Sandbox create/lifecycle, P21-09 Vercel exec and files, P21-10 sync-back (R02.31).
  Stand in B1's wrapper, staging, records and fakes until integration.
- R1: P22-01..09 (dataset loader + hash, toolset distributions, trajectory converter, stats,
  checkpoint, durable batch files + resume, per-prompt worker, batch orchestration, `indus research`
  CLI with the batch and distributions verbs; leave compress/swe/sample verbs as clear "not yet").
Later (not this wave): P21-12..14, 16; P22-10..15.
Cross-slice: test-only stand-ins under tests/.../_stubs_<slice>.py with FIRST LINE "# STAND-IN:
removed at integration" (never in src/).
Vendor SDKs (modal, daytona, vercel) go in optional extras with exact pins (uploaded ≥14 days before
the exclude-newer date), imported lazily and pyright-ignored; CI installs no extras, so every test
runs on the fakes and skips cleanly without the SDK. No real cloud calls, no network.
Never put a vendor token in argv, logs or records; tokens come from the secret store / scrubbed env.
Config rows: only your tasks' rows in foundation/config/schema.py and its test (list them as shared).
D-ids: do not number them; list intended differences under "Decisions for the lead" in your notes.
Leave uv.lock to the lead.
Every task: note first in docs/notes/<phase>/<slug>.md; Read the reference paths in the row, close
them, write; Compare after; tests per the Verify column (fakes, fake clock, tmp homes only — never
write under the real ~ or /root, no /tmp outside the test's tmp dir); update only your trap/ACC rows.
No credential-shaped literals.
Before finishing: uv run ruff check <paths>, uv run ruff format --check <paths>, uv run pyright <paths>,
uv run lint-imports, uv run pytest <your test dirs> -q -p no:cacheprovider -m "not perf",
uv run python -m tooling.checks --require-ref.
Usage: the lead may message you to wrap up. On that message, stop starting new tasks, get what you
have to a passing state for the finished tasks, and report which tasks are done / partial.
