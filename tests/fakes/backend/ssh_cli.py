"""Fake ``ssh`` and ``scp`` executables for the SSH backend tests (P11-08..P11-12).

``install(bin_dir, remote_home)`` writes two small executables into ``bin_dir`` that run this
module. They behave like the OpenSSH client against a "remote" host that is really this machine:

- ``ssh [options] destination command...`` runs the joined command with ``bash -c`` in
  ``remote_home`` with ``HOME`` set to it, stdin and stdout passed through. Only the variables
  named by ``-o SendEnv=NAME`` options reach the remote command (as sshd with ``AcceptEnv``
  would), plus ``PATH``.
- ``ssh -O exit destination`` answers like a master that was asked to stop.
- ``scp [options] local destination:remote`` copies the file (remote paths are host paths).

Every call appends one JSON line to ``calls.jsonl`` beside the executables: the argv and the
values of the SendEnv names as the client saw them. ``FakeSsh.set_mode`` scripts failures that
apply to the next calls: ``refused``, ``hostkey``, ``auth`` (ssh's own messages, exit 255), and
``home_fail`` (a ``$HOME`` lookup that fails).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]

LAUNCHER = """#!{python}
import sys
sys.path.insert(0, {root!r})
from tests.fakes.backend.ssh_cli import main
raise SystemExit(main({tool!r}, {state!r}))
"""

REFUSED = "ssh: connect to host {host} port {port}: Connection refused"
HOSTKEY = (
    "@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@\n"
    "@    WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!     @\n"
    "@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@\n"
    "Host key verification failed."
)
AUTH = "{dest}: Permission denied (publickey)."


@dataclass
class FakeSsh:
    bin_dir: Path
    remote_home: Path
    state: Path

    @property
    def calls_file(self) -> Path:
        return self.bin_dir / "calls.jsonl"

    def calls(self) -> list[dict[str, Any]]:
        if not self.calls_file.exists():
            return []
        return [json.loads(line) for line in self.calls_file.read_text().splitlines() if line]

    def set_mode(self, mode: str, **extra: Any) -> None:
        self.state.write_text(json.dumps({"mode": mode, **extra}))

    def path_env(self, base: str = "") -> str:
        rest = base or os.defpath
        return f"{self.bin_dir}{os.pathsep}{rest}"


def install(bin_dir: Path, remote_home: Path) -> FakeSsh:
    bin_dir.mkdir(parents=True, exist_ok=True)
    remote_home.mkdir(parents=True, exist_ok=True)
    state = bin_dir / "state.json"
    state.write_text(json.dumps({"mode": "ok", "home": str(remote_home)}))
    for tool in ("ssh", "scp"):
        target = bin_dir / tool
        target.write_text(
            LAUNCHER.format(python=sys.executable, root=str(REPO_ROOT), tool=tool, state=str(state))
        )
        target.chmod(0o755)
    return FakeSsh(bin_dir, remote_home, state)


def is_home_lookup(command: str) -> bool:
    """The backend's remote home question (and nothing else that mentions ``$HOME``)."""
    return command.startswith("printf ") and command.endswith('"$HOME"')


def _parse(argv: list[str]) -> tuple[dict[str, list[str]], list[str]]:
    """(options by flag, positional words) for ssh/scp argv."""
    options: dict[str, list[str]] = {}
    rest: list[str] = []
    with_value = {"-o", "-p", "-P", "-i", "-O", "-l", "-F"}
    i = 0
    while i < len(argv):
        word = argv[i]
        if not rest and word in with_value and i + 1 < len(argv):
            options.setdefault(word, []).append(argv[i + 1])
            i += 2
            continue
        if not rest and word.startswith("-") and len(word) > 1:
            options.setdefault(word, []).append("")
            i += 1
            continue
        rest = argv[i:]
        break
    return options, rest


def _record(state_path: Path, tool: str, argv: list[str], sent: dict[str, str | None]) -> None:
    line = json.dumps({"tool": tool, "argv": argv, "sent_env": sent})
    with open(state_path.parent / "calls.jsonl", "a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def main(tool: str, state_file: str) -> int:
    state_path = Path(state_file)
    state: dict[str, Any] = json.loads(state_path.read_text())
    argv = sys.argv[1:]
    options, words = _parse(argv)
    names = [o.split("=", 1)[1] for o in options.get("-o", []) if o.startswith("SendEnv=")]
    sent = {name: os.environ.get(name) for name in names}
    _record(state_path, tool, argv, sent)
    mode = state.get("mode", "ok")
    home = state["home"]
    dest = words[0] if words else ""
    host = dest.rsplit("@", 1)[-1]
    port = (options.get("-p") or options.get("-P") or ["22"])[0]
    if mode == "refused":
        print(REFUSED.format(host=host, port=port), file=sys.stderr)
        return 255
    if mode == "hostkey":
        print(HOSTKEY, file=sys.stderr)
        return 255
    if mode == "auth":
        print(AUTH.format(dest=dest), file=sys.stderr)
        return 255
    if tool == "scp":
        local, remote = words[0], words[1].split(":", 1)[1]
        shutil.copyfile(local, remote)
        return 0
    if "-O" in options:
        print("Exit request sent.", file=sys.stderr)
        return 0
    command = " ".join(words[1:])
    if mode == "home_fail" and is_home_lookup(command) and state.get("home_fail", 0) > 0:
        state["home_fail"] = int(state["home_fail"]) - 1
        state_path.write_text(json.dumps(state))
        print("bash: HOME lookup failed", file=sys.stderr)
        return 1
    env = {"HOME": home, "PATH": os.environ.get("PATH", os.defpath), "LANG": "C.UTF-8"}
    env.update({k: v for k, v in sent.items() if v is not None})
    done = subprocess.run(["bash", "-c", command], cwd=home, env=env, check=False)  # noqa: S603
    return done.returncode


if __name__ == "__main__":  # pragma: no cover - the launcher calls main directly
    raise SystemExit(main(sys.argv[1], sys.argv[2]))


def redirect_remote_tmp(monkeypatch: Any, directory: Path) -> Path:
    """Make every SSH environment opened in this test use ``directory`` as its remote temp dir
    (the fake's "remote" /tmp would otherwise be this machine's shared /tmp)."""
    import dataclasses

    from indus.backends.ssh import environment as ssh_environment

    directory.mkdir(parents=True, exist_ok=True)
    original = ssh_environment.settings_from

    def settings_from(scope: Any, overrides: Any = None) -> Any:
        return dataclasses.replace(original(scope, overrides), temp_dir=str(directory))

    monkeypatch.setattr(ssh_environment, "settings_from", settings_from)
    return directory
