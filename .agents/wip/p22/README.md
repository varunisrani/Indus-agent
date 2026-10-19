# P22 research tooling — slice reports (base 730cf11)

## R1 — w19-p22r1.patch — DONE (P22-01..09 dataset loader + hash, distributions, trajectory converter + stats, checkpoint, batch files + resume, worker, orchestration, `indus research`)
- New: research/{dataset,distributions,trajectory,stats,checkpoint,batch_io,worker,batch}.py,
  cli/commands/research.py, tests (unit/research ×8, integration/research/test_batch_run.py — real
  subprocess, 4 workers, SIGKILL + --resume: 37 then exactly 63 more, no repeats; cli test_research),
  fake tests/fakes/research/routed_model.py, fixtures (3 datasets, 5 trajectories + goldens, 3 model
  scripts), notes ×9, docs/user/reference/trajectory-format.md (.gitignore `!docs/user/reference/`).
- Shared: cli/app.py `research` row (last); help-order golden; non_tty_table (group + 5 verbs);
  config ResearchSection research.runs_root ./research-runs (+test); traps R10.1–R10.7.
- LEAD DECISION (layering): cli/commands/research.py reaches indus.research via importlib by name
  because research is a sibling of cli in the import contract; P22-10 (chat --save-trajectories)
  needs the same. Preferred: move indus.research one layer below the surfaces (pyproject contract,
  tests/unit/test_layers.py, 00-layout doc) and import it normally.
- Wiring: worker.ImagePrep.register is the P21-14 seam (task id task_<index>, row image/cwd); until
  then rows naming an image fail with zero model calls and the parent warns up front.
- Decisions (need D-ids): checkpoint indices skipped only for the same dataset hash, --resume adds the
  content scan and records found prompts; batch numbers restart at 0 per run (append); --batch-size
  10; workers get the env view through the pool pipe; partial runs (max_iterations, budget_exhausted,
  invalid_tool_strikes, guardrail_halt) written with partial: true, provider failures/interruptions
  retried; missing docker CLI under Docker backend = failure; provider routing in extra_body.provider
  whenever given; converter system turn in our own words.
- Follow-ups: converter goldens change only with a note entry; an orphaned worker can finish its
  current prompt after the parent is killed.
