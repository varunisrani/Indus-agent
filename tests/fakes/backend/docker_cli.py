"""A fake container client: ``docker`` without a daemon (P11-01).

State lives in a JSON file (daemon up or down, images, containers with labels, the host facts
``info`` reports); every call is appended to ``calls.jsonl`` with its argv and its environment, so
tests can check that a value travelled in the client env and never on the command line.

Two ways in:

- as an executable: ``FakeDocker.install()`` writes a ``docker`` shim that runs this file with
  ``--state <path>``. ``exec`` really runs the command (``bash -c ...``) on this machine with the
  container's variables, so the shared shell flow (snapshot, wrapper, cwd marker, kill) is
  exercised for real. Paths are not translated: tests give the environment a host directory as
  its working directory.
- in process: ``FakeDocker.runner`` / ``sync_runner`` answer every other subcommand without a
  subprocess.

The module uses the standard library only when it runs as a script.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DAEMON_DOWN = (
    "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. "
    "Is the docker daemon running?"
)
ZERO_TIME = "0001-01-01T00:00:00Z"
CONTAINER_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
VALUE_FLAGS = frozenset(
    {
        "--name", "--label", "-l", "-w", "--workdir", "-e", "--env", "-v", "--volume", "--tmpfs",
        "--cap-drop", "--cap-add", "--security-opt", "--cpus", "--memory", "-m", "--pids-limit",
        "--shm-size", "--storage-opt", "--user", "-u", "--network", "--net", "--mount",
        "--entrypoint", "--pull", "--hostname", "-p", "--publish", "--format", "--filter", "-t",
        "--time", "--signal",
    }
)  # fmt: skip


def default_state(root: Path) -> dict[str, Any]:
    return {
        "daemon": True,
        "version": "27.3.1",
        "info": {
            "Driver": "overlay2",
            "DriverStatus": [["Backing Filesystem", "extfs"]],
            "MemoryLimit": True,
            "PidsLimit": True,
            "CpuCfsQuota": True,
        },
        "storage_create": "ok",
        "images": {},
        "containers": {},
        "seq": 0,
        "now": "2026-10-18T12:00:00Z",
        "fail": {},
        "root": str(root / "fs"),
    }


# -- state ------------------------------------------------------------------------------------


@contextlib.contextmanager
def locked(state_path: Path) -> Iterator[dict[str, Any]]:
    """Read-modify-write the state under an exclusive lock."""
    lock = state_path.with_suffix(".lock")
    with open(lock, "a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            state: dict[str, Any] = json.loads(state_path.read_text(encoding="utf-8"))
            yield state
            state_path.write_text(json.dumps(state, indent=1), encoding="utf-8")
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def record(state_path: Path, argv: Sequence[str], env: Mapping[str, str]) -> None:
    line = json.dumps({"argv": list(argv), "env": dict(env)}) + "\n"
    with open(state_path.with_name("calls.jsonl"), "a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.write(line)
        fcntl.flock(handle, fcntl.LOCK_UN)


# -- templates ----------------------------------------------------------------------------------


def _lookup(view: Any, dotted: str) -> Any:
    node = view
    for part in [p for p in dotted.strip().lstrip(".").split(".") if p]:
        node = node.get(part) if isinstance(node, dict) else None
    return node


def _show(value: Any) -> str:
    if value is None:
        return "<no value>"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict | list):
        return json.dumps(value)
    return str(value)


def render(template: str, view: Mapping[str, Any]) -> str:
    """The few Go template forms the product uses: ``{{.A.B}}``, ``{{json .X}}`` and
    ``{{index .X "key"}}``."""

    def one(match: re.Match[str]) -> str:
        body = match.group(1).strip()
        if body.startswith("json "):
            target = body[5:].strip()
            return json.dumps(view if target == "." else _lookup(view, target))
        if body.startswith("index "):
            found = re.match(r'index\s+(\S+)\s+"([^"]*)"', body)
            if found is None:
                return ""
            table = _lookup(view, found.group(1))
            value = table.get(found.group(2)) if isinstance(table, dict) else None
            return "" if value is None else str(value)
        return _show(_lookup(view, body))

    return re.sub(r"\{\{(.*?)\}\}", one, template)


# -- argument parsing ------------------------------------------------------------------------


@dataclass
class Parsed:
    options: list[tuple[str, str | None]]
    rest: list[str]

    def values(self, *names: str) -> list[str]:
        return [v for k, v in self.options if k in names and v is not None]

    def has(self, *names: str) -> bool:
        return any(k in names for k, _ in self.options)


def parse(args: Sequence[str]) -> Parsed:
    """Options up to the first positional argument, then the rest as given."""
    options: list[tuple[str, str | None]] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if not arg.startswith("-") or arg == "-":
            break
        if arg.startswith("--") and "=" in arg:
            key, value = arg.split("=", 1)
            options.append((key, value))
        elif arg in VALUE_FLAGS:
            options.append((arg, args[i + 1] if i + 1 < len(args) else ""))
            i += 1
        else:
            options.append((arg, None))
        i += 1
    return Parsed(options, list(args[i:]))


def _env_value(spec: str, client_env: Mapping[str, str]) -> tuple[str, str | None]:
    if "=" in spec:
        name, value = spec.split("=", 1)
        return name, value
    return spec, client_env.get(spec)


# -- containers -------------------------------------------------------------------------------


def _find(state: Mapping[str, Any], ref: str) -> dict[str, Any] | None:
    containers: dict[str, dict[str, Any]] = state["containers"]
    for cid, box in containers.items():
        if ref and (cid.startswith(ref) or box["name"] == ref):
            return box
    return None


def _new_id(state: dict[str, Any]) -> str:
    state["seq"] += 1
    return hashlib.sha256(f"container-{state['seq']}".encode()).hexdigest()


def _no_container(ref: str) -> tuple[int, str, str]:
    return 1, "", f"Error response from daemon: No such container: {ref}\n"


def _ps_view(box: Mapping[str, Any]) -> dict[str, Any]:
    labels = ",".join(f"{k}={v}" for k, v in sorted(box["labels"].items()))
    return {
        "ID": box["id"][:12],
        "State": box["state"],
        "Names": box["name"],
        "Image": box["image"],
        "Labels": labels,
    }


def _inspect_view(box: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "Id": box["id"],
        "Name": "/" + box["name"],
        "State": {
            "Status": box["state"],
            "Running": box["state"] == "running",
            "FinishedAt": box["finished_at"],
        },
        "Config": {"Image": box["image"], "Labels": box["labels"]},
        "HostConfig": {"NetworkMode": box["network"]},
    }


def _filters_match(box: Mapping[str, Any], filters: Sequence[str]) -> bool:
    for spec in filters:
        key, _, value = spec.partition("=")
        if key == "label":
            name, eq, wanted = value.partition("=")
            if name not in box["labels"] or (eq and box["labels"][name] != wanted):
                return False
        elif key == "status":
            if box["state"] != value:
                return False
        elif key == "name" and value not in box["name"]:
            return False
    return True


def _create(
    state: dict[str, Any], parsed: Parsed, client_env: Mapping[str, str], status: str
) -> tuple[int, str, str]:
    networks = parsed.values("--network", "--net")
    if len(networks) > 1:
        return 125, "", "docker: conflicting options: more than one network mode given.\n"
    if not parsed.rest:
        return 125, "", "docker: requires at least 1 argument.\n"
    image = parsed.rest[0]
    if image not in state["images"] and parsed.values("--pull") == ["never"]:
        return 125, "", f"Error response from daemon: No such image: {image}\n"
    name = (parsed.values("--name") or [""])[0]
    if name and _find(state, name) is not None:
        return 125, "", f'docker: Error response from daemon: Conflict. "/{name}" is in use.\n'
    env: dict[str, str] = {}
    for spec in parsed.values("-e", "--env"):
        key, value = _env_value(spec, client_env)
        if value is not None:
            env[key] = value
    labels: dict[str, str] = {}
    for spec in parsed.values("--label", "-l"):
        key, _, value = spec.partition("=")
        labels[key] = value
    binds: dict[str, str] = {}
    for spec in parsed.values("-v", "--volume"):
        parts = spec.split(":")
        if len(parts) >= 2 and parts[0].startswith("/"):
            binds[parts[1]] = parts[0]
    cid = _new_id(state)
    state["containers"][cid] = {
        "id": cid,
        "name": name or f"auto_{cid[:6]}",
        "image": image,
        "labels": labels,
        "state": status,
        "env": env,
        "binds": binds,
        "workdir": (parsed.values("-w", "--workdir") or ["/"])[0],
        "network": (networks or ["bridge"])[0],
        "finished_at": ZERO_TIME,
        "args": [k if v is None else f"{k}={v}" for k, v in parsed.options],
        "command": parsed.rest[1:],
    }
    return 0, cid + "\n", ""


@dataclass
class ExecPlan:
    argv: list[str]
    env: dict[str, str]
    cwd: str


def _host_dir(state: Mapping[str, Any], box: Mapping[str, Any], path: str) -> str:
    if os.path.isdir(path):
        return path
    for target, source in sorted(box["binds"].items(), key=lambda kv: -len(kv[0])):
        if path == target or path.startswith(target.rstrip("/") + "/"):
            return source + path[len(target) :]
    mapped = os.path.join(state["root"], box["id"][:12], path.lstrip("/"))
    os.makedirs(mapped, exist_ok=True)
    return mapped


def _exec(
    state: Mapping[str, Any], parsed: Parsed, client_env: Mapping[str, str]
) -> tuple[int, str, str, ExecPlan | None]:
    if len(parsed.rest) < 2:
        return 1, "", "docker: exec requires at least 2 arguments.\n", None
    ref, command = parsed.rest[0], parsed.rest[1:]
    box = _find(state, ref)
    if box is None:
        code, out, err = _no_container(ref)
        return code, out, err, None
    if box["state"] != "running":
        return 1, "", f"Error response from daemon: container {ref} is not running\n", None
    env = {
        "PATH": os.environ.get("PATH", CONTAINER_PATH),
        "HOME": _host_dir(state, box, "/root"),
        "HOSTNAME": box["id"][:12],
        "LANG": "C.UTF-8",
    }
    env.update(box["env"])
    for spec in parsed.values("-e", "--env"):
        key, value = _env_value(spec, client_env)
        if value is not None:
            env[key] = value
    workdir = (parsed.values("-w", "--workdir") or [box["workdir"]])[0]
    return 0, "", "", ExecPlan(command, env, _host_dir(state, box, workdir))


def handle(
    state_path: Path, args: Sequence[str], client_env: Mapping[str, str]
) -> tuple[int, str, str, ExecPlan | None]:
    """Answer one client call: (exit status, stdout, stderr, what ``exec`` should run)."""
    record(state_path, ["docker", *args], client_env)
    if not args:
        return 0, "Usage: docker [OPTIONS] COMMAND\n", "", None
    sub, rest = args[0], list(args[1:])
    if sub == "image" and rest:
        sub, rest = "image " + rest[0], rest[1:]
    with locked(state_path) as state:
        failure = state["fail"].get(sub)
        if sub == "version":
            client = f"Client:\n Version:           {state['version']}\n API version:       1.47\n"
            if not state["daemon"]:
                return 1, client, DAEMON_DOWN + "\n", None
            server = f"\nServer:\n Engine:\n  Version:          {state['version']}\n"
            return 0, client + server, "", None
        if not state["daemon"]:
            return 1, "", DAEMON_DOWN + "\n", None
        if failure:
            return 1, "", f"Error response from daemon: {failure}\n", None
        parsed = parse(rest)
        if sub == "info":
            fmt = (parsed.values("--format") or [""])[0]
            return 0, render(fmt, state["info"]) + "\n" if fmt else "Server: fake\n", "", None
        if sub == "image inspect":
            if not parsed.rest or parsed.rest[0] not in state["images"]:
                ref = parsed.rest[0] if parsed.rest else ""
                return 1, "", f"Error response from daemon: No such image: {ref}\n", None
            image = state["images"][parsed.rest[0]]
            view = {"Id": image["id"], "Config": {"Entrypoint": image["entrypoint"]}}
            fmt = (parsed.values("--format") or ["{{json .}}"])[0]
            return 0, render(fmt, view) + "\n", "", None
        if sub == "run":
            if not parsed.has("-d", "--detach"):
                return 0, "", "", None  # throwaway probe containers: nothing to keep
            code, out, err = _create(state, parsed, client_env, state.get("run_state", "running"))
            return code, out, err, None
        if sub == "create":
            if parsed.values("--storage-opt"):
                mode = state["storage_create"]
                if mode == "reject":
                    return (
                        125,
                        "",
                        "Error response from daemon: --storage-opt is supported only for "
                        "overlay over xfs with 'pquota' mount option\n",
                        None,
                    )
                if mode != "ok":
                    return 125, "", f"Error response from daemon: {mode}\n", None
            code, out, err = _create(state, parsed, client_env, "created")
            return code, out, err, None
        if sub == "ps":
            show_all = parsed.has("-a", "--all")
            filters = parsed.values("--filter")
            fmt = (parsed.values("--format") or ["{{.ID}}"])[0]
            lines = [
                render(fmt, _ps_view(box))
                for box in state["containers"].values()
                if (show_all or box["state"] == "running") and _filters_match(box, filters)
            ]
            return 0, "".join(line + "\n" for line in lines), "", None
        if sub == "inspect":
            fmt = (parsed.values("--format") or ["{{json .}}"])[0]
            rendered: list[str] = []
            for ref in parsed.rest:
                box = _find(state, ref)
                if box is None:
                    return 1, "", f"Error: No such object: {ref}\n", None
                rendered.append(render(fmt, _inspect_view(box)))
            return 0, "".join(line + "\n" for line in rendered), "", None
        if sub in ("start", "stop", "rm", "kill"):
            for ref in parsed.rest:
                box = _find(state, ref)
                if box is None:
                    code, out_text, err = _no_container(ref)
                    return code, out_text, err, None
                if sub == "start":
                    box["state"] = "running"
                elif sub in ("stop", "kill"):
                    if box["state"] == "running":
                        box["state"] = "exited"
                        box["finished_at"] = state["now"]
                else:
                    if box["state"] == "running" and not parsed.has("-f", "--force"):
                        return (
                            1,
                            "",
                            f"Error response from daemon: cannot remove container {ref}: "
                            "container is running: stop the container before removing or "
                            "force remove\n",
                            None,
                        )
                    del state["containers"][box["id"]]
            return 0, "".join(r + "\n" for r in parsed.rest), "", None
        if sub == "exec":
            return _exec(state, parsed, client_env)
        return 1, "", f"docker: '{sub}' is not a docker command.\n", None


def main() -> int:
    args = sys.argv[1:]
    if len(args) < 2 or args[0] != "--state":
        sys.stderr.write("fake docker: missing --state\n")
        return 2
    state_path, rest = Path(args[1]), args[2:]
    code, out, err, plan = handle(state_path, rest, dict(os.environ))
    if plan is not None:
        os.chdir(plan.cwd)
        os.execvpe(plan.argv[0], plan.argv, plan.env)  # noqa: S606 - the fake container
    sys.stdout.write(out)
    sys.stderr.write(err)
    return code


# -- the test-side handle -------------------------------------------------------------------------


class FakeDocker:
    """A fake client with its state under ``root`` (see the module notes)."""

    def __init__(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        self.state_path = root / "state.json"
        self.state_path.write_text(json.dumps(default_state(root)), encoding="utf-8")
        self.bin_dir = root / "bin"
        self.on_call: Callable[[Sequence[str]], None] | None = None

    # -- installation

    def install(self, name: str = "docker") -> Path:
        """A ``name`` shim in ``bin_dir`` that runs this module; returns its path."""
        self.bin_dir.mkdir(exist_ok=True)
        shim = self.bin_dir / name
        here = Path(__file__).resolve()
        shim.write_text(
            "#!/bin/sh\n"
            f"exec {shlex.quote(sys.executable)} -I {shlex.quote(str(here))} "
            f'--state {shlex.quote(str(self.state_path))} "$@"\n',
            encoding="utf-8",
        )
        shim.chmod(0o755)
        return shim

    # -- answering in process

    def answer(
        self, argv: Sequence[str], timeout: float, env: Mapping[str, str]
    ) -> tuple[int, str, str, bool]:
        if self.on_call is not None:
            self.on_call(argv)
        code, out, err, plan = handle(self.state_path, list(argv[1:]), env)
        if plan is None:
            return code, out, err, False
        try:
            done = subprocess.run(  # noqa: S603 - the fake container
                plan.argv,
                cwd=plan.cwd,
                env=plan.env,
                capture_output=True,
                text=True,
                timeout=timeout,
                stdin=subprocess.DEVNULL,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return -1, "", "timed out", True
        return done.returncode, done.stdout, done.stderr, False

    def sync_runner(self, argv: Sequence[str], timeout: float, env: Mapping[str, str]) -> Any:
        from indus.backends.docker.probe import CliResult

        code, out, err, timed_out = self.answer(argv, timeout, env)
        return CliResult(code, out, err, timed_out=timed_out)

    async def runner(self, argv: Sequence[str], timeout: float, env: Mapping[str, str]) -> Any:
        return self.sync_runner(argv, timeout, env)

    # -- state helpers

    def state(self) -> dict[str, Any]:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    @contextlib.contextmanager
    def edit(self) -> Iterator[dict[str, Any]]:
        with locked(self.state_path) as state:
            yield state

    def calls(self) -> list[dict[str, Any]]:
        path = self.state_path.with_name("calls.jsonl")
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def commands(self) -> list[list[str]]:
        """Every call's argv without the program name."""
        return [call["argv"][1:] for call in self.calls()]

    def subcommands(self) -> list[str]:
        return [argv[0] if argv else "" for argv in self.commands()]

    def set_daemon(self, up: bool) -> None:
        with self.edit() as state:
            state["daemon"] = up

    def add_image(self, image: str, entrypoint: list[str] | None = None) -> None:
        with self.edit() as state:
            digest = hashlib.sha256(image.encode()).hexdigest()
            state["images"][image] = {"id": f"sha256:{digest}", "entrypoint": entrypoint}

    def add_container(
        self,
        *,
        labels: Mapping[str, str],
        state_name: str = "running",
        finished_at: str = ZERO_TIME,
        image: str = "img",
        network: str = "bridge",
        name: str = "",
    ) -> str:
        with self.edit() as state:
            cid = _new_id(state)
            state["containers"][cid] = {
                "id": cid,
                "name": name or f"pre_{cid[:6]}",
                "image": image,
                "labels": dict(labels),
                "state": state_name,
                "env": {},
                "binds": {},
                "workdir": "/",
                "network": network,
                "finished_at": finished_at,
                "args": [],
                "command": ["sleep", "infinity"],
            }
            return cid

    def remove_container(self, cid: str) -> None:
        with self.edit() as state:
            state["containers"].pop(cid, None)

    def containers(self) -> dict[str, dict[str, Any]]:
        return self.state()["containers"]


if __name__ == "__main__":
    sys.exit(main())
