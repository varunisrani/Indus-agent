# Adding a terminal backend

A backend decides where the agent's commands run. Each one is a row in the backend registry
(`indus.backends.registry`): a name, an async factory that builds an `Environment`, flags that
describe it, and an optional probe that says whether it can run here.

## The contract

An `Environment` (`indus.backends.contract`) runs one shell command at a time and answers with an
`ExecResult`: output, exit code and whether it timed out or was interrupted. It must

- honour the timeout it is given (exit code 124) and the turn's cancel signal (130), stopping the
  whole process tree;
- keep the working directory, and exported variables when the backend supports a persistent shell,
  from one command to the next;
- raise `EnvironmentConnectionError` when the connection is lost, so the registry can rebuild the
  environment and the model gets a "degraded, try again" answer instead of a traceback;
- release everything in `cleanup`.

The shared contract suite (`tests/contract/backends/`) runs the same cases against every backend:
echo, working directory, timeouts, interrupts, large output, background processes.

## Registering it

Built-in backends are listed in `default_entries()`; a plugin calls
`ctx.register_terminal_backend(...)`. The backend is then chosen with `terminal.backend: <name>`
and appears in `indus setup terminal`. Settings belong in the `terminal` section with a prefix of
the backend's name; credentials are read through the scope's secrets, never from the process
environment directly.

## Sandboxes

Container and cloud backends usually keep one environment per profile or per task, upload the files
listed in `terminal.credential_files` read-only, and can copy changed files back at cleanup. An idle
environment is reaped after `terminal.lifetime_s`.
