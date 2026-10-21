# Research tooling

`indus research` runs the agent over many prompts and turns the conversations into training and
evaluation data. It is meant for people who study or fine-tune agents; everyday use never needs
it. The dataset commands need the `research` extra.

| Command | What it does |
|---|---|
| `indus research batch DATASET` | run every prompt of a JSONL file through the agent and save each conversation as a trajectory |
| `indus research distributions` | list the toolset distributions a batch can sample from |
| `indus research swe` | run coding tasks with only a terminal tool and save their trajectories |
| `indus research compress INPUT` | shorten trajectories to fit a token budget |
| `indus research sample` | draw long trajectories from datasets, compress them and merge them into one file |

## A batch run

```bash
indus research batch prompts.jsonl --run-name first --workers 4 --distribution default
```

Each line of `prompts.jsonl` holds a `"prompt"`. Every prompt runs in its own isolated session with
a toolset drawn from the distribution (so the data covers different tool mixes), and the result is
written to the run's folder as one trajectory per line. Runs resume: start the same command again
and finished prompts are skipped. Progress, failures and token counts are kept per run, and a
statistics file sums up how often each tool was used and how often it failed.

The trajectory files follow the [trajectory format](../reference/trajectory-format.md). A normal
chat can write the same samples as you work: `indus chat --save-trajectories` (or
`research.save_trajectories: true`) appends each finished turn's conversation to
`trajectory_samples.jsonl` in the working folder.

## Compressing

`indus research compress` keeps the start and the last turns of a long trajectory and replaces as
few middle turns as needed with one summary turn, never separating a tool call from its result,
until it fits `--target-max-tokens`, counted with a pinned tokenizer
(`--tokenizer`, `--revision`). Remote tokenizer code never runs unless you pass
`--trust-remote-code`. `--dry-run` shows what would happen.

## Experiments

To compare two versions of the agent on the same tasks, see [Evals](../research/evals.md).
