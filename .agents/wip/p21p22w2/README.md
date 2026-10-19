# P21 rest (B3) + P22 rest (R2): parked builder patches

Base for both: adbbe87. Apply with `git apply --3way` onto main, then run the full gate.

## w21-p22r2.patch: P22-10..15 (saver, compress config/compressor/runner, SWE runner, eval kit, sample)
- Builder checks: 1851 passed / 2 skipped on research+cli+config paths; ruff, pyright, lint-imports, tooling.checks ok.
- New extra `research = ["datasets==5.0.1", "transformers==5.17.0"]` (Apache-2.0). uv.lock NOT regenerated: lead re-locks and checks transitive licences.
- Stand-in: `tests/unit/research/_stubs_r2.py` stands in for P21-14. At integration, plug the P21-14 registry into
  `swe_runner.OverrideRegistrar` (via `RegistryEnvironments(registrar=...)`) and `worker.ImagePrep.register`, then delete the stub.
- Decisions: rejects file `<stem>.rejects.jsonl` (file mode) / `<dir>.rejects.jsonl` (folder mode); `skip_under_target: false`
  drops under-target entries; saver listener is synchronous (not wired into TUI bridge or gateway); seeded random instead of
  Hypothesis; blank summary = failure, retried; SWE runner refuses non-terminal tool calls; non-zero exit is not a tool error in
  trace metrics; sampled rows tagged `sampled_from`/`sampled_tokens`; SweRunner registry shuts down with `fence=False`;
  `test_saver.py` resets the fence after each test.
- Follow-up: `swe` has no per-backend `terminal.docker_image` flag (uses profile value unless `--image`).

## w21-p21b3.patch: P21 rest (pending)
