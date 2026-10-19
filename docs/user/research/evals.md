# Running experiments on the agent

`indus.research.evals.kit` holds the conventions every A/B experiment on Indus Agent follows. An
experiment answers one question ("does this change cut wasted tool calls?") by running the same
tasks against two arms and comparing what can be measured. The kit is a library: an experiment is
a small script that builds its arms, tasks and oracles and hands them to the kit.

## Arms differ in one thing

An arm is a name, an optional code tree to put on `PYTHONPATH` (for example a worktree of `main`
and a worktree of the branch under test) and optional config keys. `check_arms(baseline,
variant)` returns the one thing the two arms vary and refuses anything else: two differences (a
different code tree *and* a different config key), or none at all. Everything else stays the
same across arms: the model, the tasks, the number of repetitions, a fresh home per cell.
`Arm.env(base)` gives the environment for an arm's run (pass the scope's env view, never the
process environment).

## Cells, and a store that resumes

A cell is one arm × one task × one repetition. Its id is a hash of the three, so it is the same
on every run. `ResultsStore(folder)` keeps `results.jsonl` in the experiment folder:

- `run(cells, run_cell)` runs only the cells that are not in the file yet, so an interrupted
  experiment picks up where it stopped and never pays twice for a finished cell;
- each row is appended and flushed to disk before the next cell starts;
- a **startup crash** (non-zero exit with empty output: the run never really started) is not
  recorded, so the cell runs again next time instead of polluting the results. A run that failed
  *after* starting (non-zero exit with output) is a real data point and is recorded.

## Score with oracles, not opinions

Each task is built to trigger one known failure, and is scored by a check a program can make:

| Oracle | Passes when |
|---|---|
| `marker(text)` | the final answer contains `text` |
| `matches(pattern, expect=...)` | the regex matches the final answer (and its first group equals `expect`) |
| `file_check(path, contains=...)` | the file exists under the cell's work folder (and holds `contains`) |

Never ask the model whether it succeeded.

## Measure from traces

`trace_metrics(rows)` computes, from one session's stored message rows: model calls, tool calls,
tool errors (judged like the batch statistics: a JSON `error` or `success: false`, or text that
starts with `error:`; a non-zero exit code alone is not an error), retries (a call to the tool
whose previous result failed), the bytes of tool results fed back to the model, and wall time
(first to last message).

## Never touch live data

Experiments read session stores only through `open_store_copy(path, run_dir=..., live=...)`. It
refuses a path outside the experiment's run folder and a path that is the live store itself
(same file, even through a hard link: the inode is compared), and opens the copy read-only. Copy
the store into the run folder first. Private transcripts never go into the repository; use
synthetic ones for anything that runs in CI.

## Observed is not modelled

Every number is a `Figure` labelled `observed` (recomputed from recorded runs) or `modelled`
(from a replay, a simulation or an estimate). `total()` refuses to add figures with different
labels, and `Report.render()` prints the two kinds in separate sections, so a modelled saving is
never presented as if it had been measured.

## Building data sets: `indus research sample`

`indus research sample` draws long trajectories from datasets and compresses them for training:

```bash
indus research sample --dataset my-org/agent-runs --dataset my-org/more-runs \
    --samples 2500 --min-tokens 16000 --seed 42 --out data --name long_runs
```

There is no default dataset: name them with `--dataset` (repeatable) or list them under
`research.sample_datasets` in the config; with neither the command exits 2. Rows whose
conversations reach `--min-tokens` are pooled, `--samples` of them are drawn with `--seed`, the
draw is compressed with `indus research compress` settings (`--config`), and the result is merged
into `<out>/<name>.jsonl` (the work files stay in `<out>/<name>.parts/`). Loading datasets and
counting tokens needs the optional `research` extra.
