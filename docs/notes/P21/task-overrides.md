# Task: Per-task overrides (P21-14) Phase: 21 Owner: P21 slice B3 builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `batch_runner.py`
(`_prepare_container_image`), `tools/terminal_tool.py` (`_resolve_container_task_id`,
`_get_env_config`, and the override registry next to them). Closed before writing. Evidence: none.

What it does (own words): some work needs one task to run somewhere other than the profile's
default sandbox: a research dataset row that names a container image or a working directory, or a
delegated task. A small process-wide registry holds, per profile and task id, an image / backend /
cwd override. The environment registry consults it for every call: a call whose task id has an
override, or whose session was bound to such a task, gets its own environment key
(`task:<task id>`), so it never reuses the default sandbox (02 §3.8 rule 1), and the factory sees
the override:

| backend | image override becomes |
|---|---|
| docker | `terminal.docker_image` for that environment only (config overlay on the scope it is built with) |
| singularity | `terminal.singularity_image = docker://<image>` (an image that already names a scheme is kept) |
| modal, daytona | the call override `image` (what their settings already read) |
| vercel_sandbox | ignored, one warning per process (Vercel picks runtimes, not images) |
| local, ssh | ignored, one warning per process (they run no image) |

`cwd` becomes the call override `cwd` (all factories read it); `backend` becomes the call override
`backend` (the registry's `backend_name` reads it).

Inputs → outputs (`src/indus/backends/task_overrides.py`):
- `TaskOverride(image=None, backend=None, cwd=None)`; `.empty`.
- `TaskOverrides`: `set(scope, task_id, override)`, `bind(scope, session_id, task_id)` (a session
  that runs the task: the batch runner's session, a delegated child), `get(scope, task_id)`,
  `resolve(scope, ident) -> (task_id, TaskOverride) | None` (ident is a task id or a bound session
  id), `clear(scope, task_id) -> bool` (drops the override and its bindings). Keys are
  `(profile home, id)` (R02.8). `TASK_OVERRIDES` is the process's registry.
- `plan(scope, task_id, override) -> TaskPlan(scope, overrides)`: the scope the factory gets
  (with the config overlay) and the call overrides (always carrying `task` so the registry keys it
  per task, even when the image was ignored).
- `register_row(scope, task_id, image, cwd) -> str | None`: the P22 seam's registrar (stores the
  row's image and cwd; a row with neither registers nothing; returns None, the seam's "no
  problem").
- `plan(scope, task_id, override)` takes the task id so the planned call overrides carry it.
- shared: `backends/registry.py` (`key_for` / `acquire` / `release` resolve registered overrides
  when the call passes none; the factory gets the planned scope); `research/worker.py`
  (`ImagePrep.register` defaults to the task-override registrar; `SessionAgentRunner` binds its
  session to the task id before the first turn, releases the task's environment and clears the
  override when the conversation ends); `research/batch.py` (the up-front warning about image rows
  now says they run in their own sandbox); `tests/unit/research/test_worker.py` (the "refused until
  P21-14" test now checks the override is registered).

Edge cases:
- Override registered for profile A is invisible to profile B (same task id).
- An explicit `overrides` mapping on a call wins over the registry.
- Clearing the task drops the bindings, so a later call of the same session id goes back to its
  default key.
- A pinned Docker image (`terminal.docker_image_pinned`) cannot be lifted by a task: under the
  Docker backend a pinned profile keeps its own image and the task's image is ignored with one
  warning (the task still gets its own sandbox). Integrator ruling at the landing (D-16.322): the
  first draft let an override image with a digest replace the pinned one, which turned a profile
  rule into something a dataset row could change. Test:
  `test_task_overrides.py::test_a_pinned_docker_image_cannot_be_lifted_by_a_task`.

Our design differences / decisions (for the lead):
- A cwd-only override also gets its own environment key (the phase rule: "overrides produce their
  own env key"); the reference collapses cwd-only overrides onto the shared sandbox.
- Docker and Singularity get the image through a config overlay on the scope the factory receives,
  so their modules did not change.
- Local and SSH ignore an image with one warning, like Vercel.

Tests planned (`tests/unit/backends/test_task_overrides.py`): mapping table per backend; env key
differs from the default and is `task:<id>`; bound session resolves to the task key; cleared after
the task (key back to the session's); profile scoping; explicit overrides win; Vercel/local warn
once; the factory receives the overlaid image for docker/singularity and `image` for modal/daytona;
the worker registrar stores the row's image and cwd.

Compare checklist (after writing):
- [x] reference maps one row image to docker/modal/daytona images and `docker://` for singularity;
  same here; Vercel ignored with a warning (phase rule).
- [x] reference clears overrides, cwd record and alias at rollout end; ours clears the override and
  its session bindings and releases the task environment.
- [x] reference keys overrides by a routed-profile-qualified id; ours by profile home.
