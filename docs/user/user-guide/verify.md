# Checking a project (`indus verify`)

`indus verify` works out how to build, test and start a project from its files, then does it.

```bash
indus verify                    # the current folder
indus verify ~/src/app --detect-only     # only show what it found
indus verify --phase test       # only the tests
```

## What it finds

The first lockfile decides the package manager (pnpm, bun, yarn or npm; uv, poetry or pipenv). A
`package.json` gives a Node recipe from its scripts; otherwise it looks for a Python project
(Django, FastAPI, Flask or plain), Go, Rust, Maven or Gradle, a `Makefile`'s targets, or a compose
file.

## What it runs

Phases run in order, stopping at the first failure: `bootstrap` (install dependencies), `build`,
`test`, then `start`, which boots the start command on 127.0.0.1 and waits for it to answer, then
stops it. `--skip-start` leaves `start` out; `--phase` (repeatable) picks phases.

Commands run on the local terminal backend with the same environment and approval rules as the
agent's commands, but with nobody to ask: a command that would need your approval is refused, not run. Each command may
run `verify.phase_timeout` seconds (600; `--timeout` for one run) and the service gets
`verify.ready_timeout` seconds (60; `--ready-timeout`) to answer; `--port` names its port. For a
compose project, `verify` refuses to build or start while that project already has running
containers. The report keeps the last 2,000 characters of each command's output; `--json` prints it
as one JSON object.

Exit codes: `0` everything passed, `1` something failed or no recipe was found, `2` the path is not
a folder.

## Checking before the agent finishes

With `verify.on_stop` on, the agent runs the project's `build` and `test` before its answer is
final whenever the turn edited code files. If they fail, the agent gets the failing command's
output and goes back to work, at most `verify.max_stop_attempts` times (2) per turn.

```bash
indus config set verify.on_stop true
```

A plugin can also check the work at that point (the `pre_verify` hook); it may send the agent back
at most `verify.max_hook_nudges` times (3) per turn.
