# Task: Fake SDKs and fake CLI (P21-15) Phase: 21 Owner: P21 slice B1 builder

Read: none in the reference (our own fakes). The surfaces come from the pinned SDKs' public API
(`docs/notes/P21/sdk-versions.md`): modal 1.6.1, daytona 0.220.0, vercel-sandbox 0.7.0, and the
Apptainer/SingularityCE CLI.

What it does (own words): every P21 test, and later the shared contract suite, runs the cloud and
HPC backends without the vendor SDKs, credentials or network. Each fake is a deterministic,
in-process stand-in exposing the subset of the vendor's sync API our backends call, with the same
names and argument names, a call log, scriptable latency on an injected clock, scriptable errors by
status code, and a small file system. The fake `apptainer` is a script on PATH that records argv
and keeps instance state in a JSON file, like the fake `docker` of P11.

## Shared kit (`tests/fakes/backend/sdk_kit.py`)
- `Cloud(root, *, clock=None, allowed=None)`: one fake vendor account. `root` is a directory in the
  test's tmp dir. Fields: `calls: list[Call]` (`Call(method, args, kwargs)`), `snapshots: dict[str,
  bytes]` (id → tar), `images: dict[str, dict[str, bytes]]` (image → files at create).
- Scripting: `cloud.fail(method, status, times=1, message="")` raises the vendor-shaped error for
  `status` on the next `times` calls of `method` (method names are `"Class.method"`, e.g.
  `"Sandbox.create"`, `"Process.exec"`); `cloud.fail_with(method, exc, times=1)` raises a given
  exception; `cloud.delay(method, seconds)` makes the call block until the injected `FakeClock` has
  moved `seconds` past the call (needs `clock.allow_threaded_advance()`; the tests' worker threads
  wait in `clock.sleep_sync`); `cloud.hang(method)` blocks until `cloud.release()` (or 30 s real
  time as a safety net) and then raises `TimeoutError` — for "the vendor never answers".
- File system: `cloud.fs` (`ToyFs`). A sandbox path is used as is when it lies under `root`
  (tests pass their tmp dirs as workspace/temp dirs, as the fake docker does); any other absolute
  path maps to `root/_vfs/<path>`. `read(path) -> bytes`, `write(path, data, mode=0o600)`,
  `exists`, `size`, `remove(path, recursive=False)`, `mkdir`, `listdir`, `export_tar(path) -> bytes`
  (members relative to `path`), `import_tar(data, dest)`, `capture() -> bytes` (whole sandbox
  tree, for snapshots), `restore(data)`, `wipe()`.
- Exec: `cloud.run(argv, *, cwd, env, stdin, timeout) -> (stdout: bytes, stderr: bytes, rc)` runs
  real bash on this machine (paths are not translated; cwd under `root` or the mapped dir; env =
  `PATH`, `HOME=<root>/_home`, `LANG` plus the SDK `env` argument — never the test process env).
  `cloud.script(pattern, stdout=b"", rc=0, stderr=b"")` answers a command matching the glob
  `pattern` (over the joined argv) without running anything, first match wins.

## Fake Modal (`tests/fakes/backend/fake_modal.py`)
`FakeModal(cloud)`; `fake.module` is a module object to hand the backend as its `modal`
(`fake.install(monkeypatch)` also puts it in `sys.modules["modal"]` and `["modal.exception"]`).
- `modal.App.lookup(name, *, client=None, environment_name=None, create_if_missing=False)` → app
  (`.app_id`, `.name`); `NotFoundError` when missing and not `create_if_missing`.
- `modal.Client.from_credentials(token_id, token_secret)` → client (`AuthError` if the pair does not
  match `fake.tokens` when set).
- `modal.Image.from_registry(tag, secret=None, *, setup_dockerfile_commands=(), force_build=False,
  add_python=None)`, `modal.Image.from_id(image_id, client=None)` → image (`.object_id`, lazy: an
  unknown id fails at `Sandbox.create` with `NotFoundError`).
- `modal.Sandbox.create(*args, app=None, name=None, tags=None, image=None, env=None, secrets=None,
  timeout=300, idle_timeout=None, workdir=None, cpu=None, memory=None, block_network=False,
  verbose=False, client=None)` → sandbox; `modal.Sandbox.from_id(sandbox_id, client=None)`,
  `modal.Sandbox.from_name(app_name, name, *, environment_name=None, client=None)`.
- sandbox: `.object_id`, `.returncode`, `exec(*args, stdout=None, stderr=None, timeout=None,
  workdir=None, env=None, secrets=None, text=True, bufsize=-1, pty=False)` → process,
  `snapshot_filesystem(timeout=55, *, ttl=…)` → image whose `object_id` restores the tree,
  `terminate(*, wait=False)`, `poll()`, `wait(raise_on_termination=True)`, `get_tags()`,
  `set_tags(tags, *, client=None)`, `detach()`.
- process: `.stdout.read()` / `.stderr.read()` (str when `text=True`, else bytes; block until the
  process ends), `.stdin.write(data)`, `.stdin.write_eof()`, `.stdin.drain()`, `.returncode`,
  `poll()`, `wait()`. The process starts at `exec`; stdin bytes reach it at `drain()`. The `timeout`
  argument kills it (returncode 137, as after SIGKILL). Calls after `terminate()` raise
  `SandboxTerminatedError`.
- `modal.exception`: `Error`, `NotFoundError`, `InvalidError`, `AuthError`, `ConnectionError`,
  `ServiceError`, `ResourceExhaustedError`, `InternalError`, `TimeoutError`,
  `SandboxTimeoutError`, `ExecTimeoutError`, `SandboxTerminatedError`. Status-scripted errors carry
  `.status` as a gRPC-style enum (`FakeGrpcStatus.UNAVAILABLE` for 503, `RESOURCE_EXHAUSTED` for
  429, `INTERNAL` for 500, `NOT_FOUND` 404, `INVALID_ARGUMENT` 400, `UNAUTHENTICATED` 401) like
  the real ones.

## Fake Daytona (`tests/fakes/backend/fake_daytona.py`)
`FakeDaytona(cloud)`; `fake.module` stands in for `daytona` (`fake.install(monkeypatch)`).
- `daytona.DaytonaConfig(api_key=None, jwt_token=None, organization_id=None, api_url=None,
  target=None)`; `daytona.Daytona(config=None)` (`DaytonaAuthenticationError` 401 when the key does
  not match `fake.api_key` when set).
- `daytona.CreateSandboxFromImageParams(image, name=None, env_vars=None, labels=None, public=None,
  auto_stop_interval=None, auto_archive_interval=None, auto_delete_interval=None, resources=None,
  os_user=None, language=None, ephemeral=None, timeout=None)`,
  `daytona.CreateSandboxFromSnapshotParams(snapshot=None, …same…)`,
  `daytona.Resources(cpu=None, memory=None, disk=None, gpu=None)`, `daytona.SandboxState`
  (`STARTED`, `STOPPED`, `STARTING`, `STOPPING`, `ERROR`, `ARCHIVED`, `DESTROYED`).
- client: `create(params=None, *, timeout=60, on_snapshot_create_logs=None)`,
  `get(sandbox_id_or_name, request_timeout=None)` (`DaytonaNotFoundError` 404), `start(sandbox,
  timeout=60)`, `stop(sandbox, timeout=60)`, `delete(sandbox, timeout=60, wait=False)`, `list()`.
- sandbox: `.id`, `.name`, `.state`, `.labels`, `.env`, `.cpu`, `.memory`, `.disk`,
  `.auto_stop_interval`, `.fs`, `.process`, `start(timeout=60)`, `stop(timeout=60, force=False)`,
  `delete(timeout=60)`, `set_autostop_interval(interval, request_timeout=None)`,
  `get_user_home_dir()`, `get_work_dir()`, `refresh_data(request_timeout=None)`.
- `sandbox.process.exec(command, cwd=None, env=None, timeout=None)` → `ExecuteResponse(exit_code,
  result)`; `command` is split with shell rules (no shell of its own) and run; stdout and stderr
  together in `result`; a stopped sandbox raises `DaytonaError` 409.
- `sandbox.fs`: `upload_file(file: bytes, remote_path, timeout=1800)`, `download_file(remote_path,
  timeout=1800) -> bytes`, `delete_file(path, recursive=False, request_timeout=None)`,
  `create_folder(path, mode, request_timeout=None)`, `get_file_info(path, request_timeout=None)` →
  `FileInfo(name, size, is_dir, mode)`, `set_file_permissions(path, mode=None, owner=None,
  group=None, request_timeout=None)`, `list_files(path, depth=None, request_timeout=None)`,
  `upload_files(files, timeout=1800)` with `daytona.FileUpload(source, destination)`.
- errors: `DaytonaError(message, status_code=None)` and the subclasses `DaytonaNotFoundError`,
  `DaytonaAuthenticationError`, `DaytonaRateLimitError`, `DaytonaConflictError`,
  `DaytonaBadRequestError`, `DaytonaTimeoutError`, `DaytonaConnectionError`,
  `DaytonaInternalServerError`, `DaytonaServiceUnavailableError` (status-scripted errors pick the
  subclass by code).

## Fake Vercel Sandbox (`tests/fakes/backend/fake_vercel.py`)
`FakeVercel(cloud)`; `fake.module` stands in for `vercel.sandbox` and `fake.sync_module` for
`vercel.sandbox.sync` (`fake.install(monkeypatch)` registers `vercel`, `vercel.sandbox`,
`vercel.sandbox.sync`).
- `SyncSandboxServiceOptions(*, base_url=None, credentials_factory=None, file_transfer_timeout=None,
  region=None)`, `SandboxCredentials(token, team_id, project_id)`.
- `SyncSandboxClient.create(*, options=None, httpx_client_factory=None)` → client; the client calls
  `options.credentials_factory()` once on first use (`SandboxCredentialsError` when it raises).
- client: `create_sandbox(*, name=None, image=None, source=None, ports=None,
  execution_time_limit=None, resources=None, persistent=None, network_policy=None, env=None,
  tags=None, region=None, destroy=True)`, `get_sandbox(*, name)`, `get_or_create_sandbox(*, name,
  resume=True, image=None, source=None, …)` → `(sandbox, created)`, `resume_sandbox(*, name)`,
  `get_snapshot(*, snapshot_id)`, `close()`.
- models: `SnapshotSource(snapshot_id)`, `SandboxResources(vcpus=None, memory=None)`,
  `SandboxStatus` (`PENDING`, `RUNNING`, `STOPPING`, `STOPPED`, `FAILED`, `ABORTED`,
  `SNAPSHOTTING`), `CompletedProcess(id, name, args, cwd, session_id, started_at, returncode, stdout,
  stderr)`.
- sandbox: `.name`, `.status`, `.persistent`, `.current_snapshot_id`, `.image`, `.cwd` (default
  `/vercel/sandbox`, mapped by the toy fs), `run_process(command, args=None, *, cwd=None, env=None,
  sudo=False, kill_after=None, check=False, stdout=None, stderr=None, capture_output=False)` →
  `CompletedProcess`, `create_process(command, args=None, *, cwd=None, env=None, sudo=False,
  kill_after=None)` → process (`wait()`, `refresh()`, `terminate()`, `kill()`,
  `send_signal(sig)`, `.returncode`, `.stdout.read()`), `snapshot(*, expiration=None)` → snapshot
  (`.id`, `delete()`; the sandbox is stopped afterwards, as the vendor does), `stop()`,
  `destroy(*, delete_orphan_snapshots=False)`, `extend_execution_time_limit(duration)`,
  `session()`.
- `sandbox.fs`: `read_bytes(path, *, cwd=None)`, `read_text(path, *, cwd=None)`,
  `write_bytes(path, data, *, cwd=None, mode=None)`, `write_text(path, text, *, cwd=None,
  mode=None)`, `exists`, `is_file`, `is_dir`, `mkdir(path, *, cwd=None, recursive=True)`,
  `listdir(path, *, cwd=None)`, `remove(path, *, cwd=None, recursive=False, missing_ok=False)`,
  `rename(src, dst, *, cwd=None)`.
- errors: `SandboxError`, `SandboxApiError` (`.status_code`, `.response.status_code`),
  `SandboxCredentialsError`, `SandboxTimeoutError`, `SandboxPathNotFoundError`,
  `SandboxFilesystemWriteError`, `SandboxTerminalStateError`.

## Fake apptainer (`tests/fakes/backend/fake_apptainer.py`)
`FakeApptainer(root)`; `install(name="apptainer")` writes a shim in `root/bin` (also
`install("singularity")`); state in `root/state.json`, calls in `root/calls.jsonl` (argv + the
`APPTAINERENV_*`/`SINGULARITYENV_*` names it saw, never other env values).
- `version` → `apptainer version 1.3.4` (or `singularity-ce version 4.1.0` for the singularity
  shim); `fake.set_version_fails(True)` makes it exit 255.
- `build [--force] <sif> <source>` writes `<sif>` (records the source); `fake.build_delay_s = N`
  sleeps N real seconds first; `fake.build_fails(stderr)` exits 255 with the text on stderr.
- `instance start [flags] <image> <name>` records flags/binds/overlay; refuses a missing image file
  that is not `docker://…`; duplicate names exit 255.
- `instance list` (text table), `instance stop <name>` (unknown → exit 255).
- `exec instance://<name> <argv…>` runs argv on this machine (no path translation) with a clean
  env (`PATH`, `HOME`, `LANG`) plus `APPTAINERENV_X`/`SINGULARITYENV_X` as `X`; an unknown or
  stopped instance exits 255 with `FATAL: …`.

Files: the four fake modules, `tests/fakes/backend/sdk_kit.py`,
`tests/fakes/backend/sdk_surfaces.json`, `scripts/dev/sdk_surfaces.py`,
`tests/unit/fakes/test_fake_sdks.py`; extras in `pyproject.toml` (shared).

Edge cases / failure modes:
- A fake public method missing from the pinned SDK's surface fails the self-test (the fakes cannot
  drift into an API the vendor does not have).
- Hanging calls are released at fixture teardown so no thread outlives its test by more than the
  30 s safety net.
- File paths outside the test's root never touch the host: they map below `root/_vfs`.

Our design differences / decisions:
- Fakes model the sync APIs only (our wrapper runs vendor calls on worker threads); `.aio` variants
  are not faked.
- Exec runs real bash (as the docker/ssh fakes do) so the shared shell flow is exercised for real.

Tests we will write (`tests/unit/fakes/test_fake_sdks.py`):
- each fake's public names ⊆ the pinned surface in `sdk_surfaces.json`
- latency honours the fake clock; hang + release; status-scripted errors (vendor-shaped, with codes)
- toy fs round trip, tar export/import, snapshot capture/restore
- fake apptainer: version, build, instance start/list/stop, exec runs and passes `APPTAINERENV_*`

## Decisions for the lead
- The Vercel extra pins `vercel-sandbox` (the package that provides `vercel.sandbox`), not the
  umbrella `vercel` package.
- `scripts/dev/sdk_surfaces.py` needs the three SDKs installed; it imports them (vendor code runs),
  so it is run by hand in a throwaway venv, never in CI.

## Status

Done (2026-10-19).
