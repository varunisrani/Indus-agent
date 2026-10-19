"""A fake ``apptainer`` / ``singularity`` command line (P21-15).

State lives in a JSON file (instances, builds, scripted failures); every call is appended to
``calls.jsonl`` with its argv and the names (never the values) of the ``APPTAINERENV_*`` /
``SINGULARITYENV_*`` variables the client saw. ``FakeApptainer.install()`` writes a shim on a
``bin`` dir that runs this file with ``--state <path> --prog <name>``.

Subcommands: ``version``; ``build [--force] <sif> <source>`` (writes the file; scripted failure
and delay); ``instance start [flags] <image> <name>`` (records flags, ``--bind`` and ``--overlay``);
``instance list``; ``instance stop <name>``; ``exec [--pwd D] instance://<name> <argv…>``, which
replaces itself with ``argv`` on this machine (paths are not translated) with a clean environment:
``PATH``, ``HOME`` (a per-instance dir under the fake's root), ``LANG``, plus every
``<PREFIX>ENV_X`` as ``X``. An unknown or stopped instance exits 255 with ``FATAL: …``.

The module uses the standard library only when it runs as a script.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shlex
import sys
import threading
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

VERSIONS = {"apptainer": "apptainer version 1.3.4", "singularity": "singularity-ce version 4.1.0"}
ENV_PREFIXES = ("APPTAINERENV_", "SINGULARITYENV_")
VALUE_FLAGS = frozenset(
    {
        "--overlay",
        "-o",
        "--bind",
        "-B",
        "--cpus",
        "--memory",
        "--pwd",
        "--env",
        "--home",
        "-H",
        "--mount",
        "--network",
        "--security",
        "--workdir",
        "-W",
        "--env-file",
        "--cpu-shares",
        "--memory-reservation",
        "--pids-limit",
        "--memory-swap",
    }
)
CLEAN_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def default_state(root: Path) -> dict[str, Any]:
    return {
        "root": str(root),
        "instances": {},
        "builds": [],
        "version_fails": False,
        "build_fails": "",
        "build_delay_s": 0.0,
        "start_fails": "",
        "start_fails_times": 0,
    }


@contextlib.contextmanager
def locked(state_path: Path) -> Iterator[dict[str, Any]]:
    lock = state_path.with_suffix(".lock")
    with open(lock, "a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            state: dict[str, Any] = json.loads(state_path.read_text(encoding="utf-8"))
            yield state
            state_path.write_text(json.dumps(state, indent=1), encoding="utf-8")
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def record(state_path: Path, prog: str, argv: Sequence[str], env: Mapping[str, str]) -> None:
    names = sorted(k for k in env if k.startswith(ENV_PREFIXES))
    line = json.dumps({"prog": prog, "argv": list(argv), "env_names": names}) + "\n"
    with open(state_path.with_name("calls.jsonl"), "a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.write(line)
        fcntl.flock(handle, fcntl.LOCK_UN)


def parse(args: Sequence[str]) -> tuple[list[tuple[str, str | None]], list[str]]:
    options: list[tuple[str, str | None]] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if not arg.startswith("-"):
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
    return options, list(args[i:])


def _values(options: Sequence[tuple[str, str | None]], *names: str) -> list[str]:
    return [v for k, v in options if k in names and v is not None]


def _has(options: Sequence[tuple[str, str | None]], *names: str) -> bool:
    return any(k in names for k, _ in options)


def _fatal(text: str) -> tuple[int, str, str]:
    return 255, "", f"FATAL:   {text}\n"


def _build(state: dict[str, Any], args: Sequence[str]) -> tuple[int, str, str]:
    options, rest = parse(args)
    if len(rest) != 2:
        return _fatal("build requires a target and a source")
    target, source = rest
    if os.path.exists(target) and not _has(options, "--force", "-F"):
        return _fatal(
            f"Build target '{target}' already exists. Use --force if you want to overwrite it"
        )
    delay = float(state.get("build_delay_s") or 0)
    if delay:
        sys.stderr.write(f"INFO:    Starting build of {source}\n" + "x" * 2000 + "\n")
        sys.stderr.flush()
        threading.Event().wait(delay)
    failure = str(state.get("build_fails") or "")
    if failure:
        return 255, "", failure
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    Path(target).write_text(f"SIF:{source}\n", encoding="utf-8")
    state["builds"].append({"target": target, "source": source, "options": [k for k, _ in options]})
    return 0, f"INFO:    Build complete: {target}\n", ""


def _instance(state: dict[str, Any], args: Sequence[str]) -> tuple[int, str, str]:
    if not args:
        return _fatal("instance requires a subcommand")
    sub, rest = args[0], list(args[1:])
    instances: dict[str, Any] = state["instances"]
    if sub == "start":
        options, positional = parse(rest)
        if len(positional) != 2:
            return _fatal("instance start requires an image and a name")
        image, name = positional
        if "://" not in image and not os.path.exists(image):
            return _fatal(f"could not open image {image}: no such file")
        if name in instances:
            return _fatal(f"instance {name} already exists")
        if state.get("start_fails") and int(state.get("start_fails_times") or 0) > 0:
            state["start_fails_times"] = int(state["start_fails_times"]) - 1
            return 255, "", str(state["start_fails"])
        overlay = (_values(options, "--overlay", "-o") or [None])[0]
        instances[name] = {
            "image": image,
            "options": [k if v is None else f"{k}={v}" for k, v in options],
            "binds": _values(options, "--bind", "-B"),
            "overlay": overlay,
            "writable_tmpfs": _has(options, "--writable-tmpfs"),
            "cpus": (_values(options, "--cpus") or [None])[0],
            "memory": (_values(options, "--memory") or [None])[0],
        }
        return 0, "INFO:    instance started successfully\n", ""
    if sub == "list":
        lines = ["INSTANCE NAME    PID      IP    IMAGE"]
        lines += [
            f"{name}    {1000 + i}        {box['image']}"
            for i, (name, box) in enumerate(instances.items())
        ]
        return 0, "\n".join(lines) + "\n", ""
    if sub == "stop":
        _, positional = parse(rest)
        if not positional:
            return _fatal("instance stop requires a name")
        name = positional[0]
        if name not in instances:
            return _fatal(f"no instance found with name {name}")
        del instances[name]
        return 0, f"INFO:    Stopping {name} instance of {name}\n", ""
    return _fatal(f"unknown instance subcommand {sub}")


def handle(
    state_path: Path, prog: str, args: Sequence[str], env: Mapping[str, str]
) -> tuple[int, str, str, tuple[list[str], dict[str, str], str] | None]:
    """(status, stdout, stderr, (argv, env, cwd) to exec or None)."""
    record(state_path, prog, args, env)
    if not args:
        return 1, "Usage: apptainer [global options...] <command>\n", "", None
    sub, rest = args[0], list(args[1:])
    with locked(state_path) as state:
        if sub == "version":
            if state.get("version_fails"):
                return 255, "", "FATAL:   could not read configuration\n", None
            return 0, VERSIONS.get(prog, VERSIONS["apptainer"]) + "\n", "", None
        if sub == "build":
            code, out, err = _build(state, rest)
            return code, out, err, None
        if sub == "instance":
            code, out, err = _instance(state, rest)
            return code, out, err, None
        if sub == "exec":
            options, positional = parse(rest)
            if len(positional) < 2:
                code, out, err = _fatal("exec requires an image and a command")
                return code, out, err, None
            target, argv = positional[0], positional[1:]
            name = target.removeprefix("instance://")
            box = state["instances"].get(name)
            if not target.startswith("instance://") or box is None:
                code, out, err = _fatal(f"no instance found with name {name}")
                return code, out, err, None
            home = Path(state["root"]) / "instance-home" / name
            home.mkdir(parents=True, exist_ok=True)
            run_env = {"PATH": env.get("PATH", CLEAN_PATH), "HOME": str(home), "LANG": "C.UTF-8"}
            prefix = "SINGULARITYENV_" if prog == "singularity" else "APPTAINERENV_"
            for key, value in env.items():
                for accepted in (prefix, *ENV_PREFIXES):
                    if key.startswith(accepted) and key[len(accepted) :]:
                        run_env[key[len(accepted) :]] = value
                        break
            cwd = (_values(options, "--pwd") or [str(home)])[0]
            return 0, "", "", (argv, run_env, cwd)
    return 255, "", f'FATAL:   unknown command "{sub}" for "{prog}"\n', None


def main() -> int:
    args = sys.argv[1:]
    if len(args) < 4 or args[0] != "--state" or args[2] != "--prog":
        sys.stderr.write("fake apptainer: missing --state/--prog\n")
        return 2
    state_path, prog, rest = Path(args[1]), args[3], args[4:]
    code, out, err, plan = handle(state_path, prog, rest, dict(os.environ))
    if plan is not None:
        argv, env, cwd = plan
        os.chdir(cwd)
        os.execvpe(argv[0], argv, env)  # noqa: S606 - the fake instance
    sys.stdout.write(out)
    sys.stderr.write(err)
    return code


# -- the test-side handle -------------------------------------------------------------------------


class FakeApptainer:
    """A fake CLI with its state under ``root`` (see the module notes)."""

    def __init__(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        self.state_path = root / "state.json"
        self.state_path.write_text(json.dumps(default_state(root)), encoding="utf-8")
        self.bin_dir = root / "bin"

    def install(self, name: str = "apptainer") -> Path:
        """A ``name`` shim in ``bin_dir``; returns its path."""
        self.bin_dir.mkdir(exist_ok=True)
        shim = self.bin_dir / name
        here = Path(__file__).resolve()
        shim.write_text(
            "#!/bin/sh\n"
            f"exec {shlex.quote(sys.executable)} -I {shlex.quote(str(here))} "
            f'--state {shlex.quote(str(self.state_path))} --prog {shlex.quote(name)} "$@"\n',
            encoding="utf-8",
        )
        shim.chmod(0o755)
        return shim

    def path_env(self, rest: str) -> str:
        return f"{self.bin_dir}{os.pathsep}{rest}"

    # -- state

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
        return [call["argv"] for call in self.calls()]

    def subcommands(self) -> list[str]:
        out: list[str] = []
        for argv in self.commands():
            if not argv:
                continue
            out.append(" ".join(argv[:2]) if argv[0] == "instance" and len(argv) > 1 else argv[0])
        return out

    def instances(self) -> dict[str, Any]:
        return self.state()["instances"]

    def builds(self) -> list[dict[str, Any]]:
        return self.state()["builds"]

    # -- scripting

    def set_version_fails(self, fails: bool = True) -> None:
        with self.edit() as state:
            state["version_fails"] = fails

    def build_fails(self, stderr: str) -> None:
        with self.edit() as state:
            state["build_fails"] = stderr

    def set_build_delay(self, seconds: float) -> None:
        with self.edit() as state:
            state["build_delay_s"] = seconds

    def start_fails(self, stderr: str, times: int = 1) -> None:
        with self.edit() as state:
            state["start_fails"] = stderr
            state["start_fails_times"] = times

    def stop_instance(self, name: str) -> None:
        """An instance that vanished behind Indus's back."""
        with self.edit() as state:
            state["instances"].pop(name, None)


if __name__ == "__main__":
    sys.exit(main())
