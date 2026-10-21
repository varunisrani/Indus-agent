"""The container's entry scripts run for real under bash and dash (P23-10, R10.25).

Each test builds a temp image root (``_INDUS_IMAGE_ROOT``) holding the three scripts, a fake s6
``/init``, and a PATH of fakes for the root-only tools (``id``, ``usermod``, ``groupmod``,
``chown``, ``groupadd``, ``getent``, ``s6-setuidgid``) and for ``indus``. The fakes record every
call in one log; ``id`` answers from a small state folder the fakes also update.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tests.unit.packaging.docker_files import DOCKER_DIR

SHELLS = [name for name in ("bash", "dash") if shutil.which(name)]
SCRIPTS = ("entrypoint.sh", "stage2.sh", "main-wrapper.sh")

FAKES: dict[str, str] = {
    "id": """
state="$FAKE_STATE"
case "$*" in
    -u) cat "$state/uid" ;;
    -g) cat "$state/gid" ;;
    "-u indus") cat "$state/indus_uid" ;;
    "-g indus") cat "$state/indus_gid" ;;
    "-G indus") cat "$state/indus_groups" ;;
    *) echo "fake id: unexpected $*" >&2; exit 2 ;;
esac
""",
    "usermod": """
echo "usermod $*" >> "$FAKE_LOG"
if [ "$1" = -o ] && [ "$2" = -u ]; then echo "$3" > "$FAKE_STATE/indus_uid"; fi
""",
    "groupmod": """
echo "groupmod $*" >> "$FAKE_LOG"
if [ "$1" = -o ] && [ "$2" = -g ]; then echo "$3" > "$FAKE_STATE/indus_gid"; fi
""",
    "chown": 'echo "chown $*" >> "$FAKE_LOG"\n',
    "groupadd": 'echo "groupadd $*" >> "$FAKE_LOG"\n',
    "getent": """
if [ -f "$FAKE_STATE/group_$2" ]; then cat "$FAKE_STATE/group_$2"; exit 0; fi
exit 2
""",
    "s6-setuidgid": """
echo "s6-setuidgid $*" >> "$FAKE_LOG"
shift
exec "$@"
""",
    "indus": """
echo "indus $* | PWD=$PWD HOME=$HOME INDUS_HOME=${INDUS_HOME:-}" >> "$FAKE_LOG"
if [ "$1" = doctor ]; then printf 'version: 1\\n' > "$INDUS_HOME/config.yaml"; fi
""",
}

INIT = 'echo "init $* | START=$INDUS_START_DIR LOGGING=${S6_LOGGING:-}" >> "$FAKE_LOG"\n'


@dataclass
class Image:
    """A temp image root plus fakes; ``run`` executes one of the scripts in it."""

    shell: str
    tmp: Path
    root: Path = field(init=False)
    data: Path = field(init=False)
    bin: Path = field(init=False)
    state: Path = field(init=False)
    log_file: Path = field(init=False)

    def __post_init__(self) -> None:
        self.root = self.tmp / "image"
        docker = self.root / "opt" / "indus" / "docker"
        docker.mkdir(parents=True)
        for name in SCRIPTS:
            shutil.copy(DOCKER_DIR / name, docker / name)
            (docker / name).chmod(0o755)
        self.write_exe(self.root / "init", INIT)
        (self.root / "command").mkdir()
        self.bin = self.tmp / "fakebin"
        self.bin.mkdir()
        for name, body in FAKES.items():
            self.write_exe(self.bin / name, body)
        self.state = self.tmp / "state"
        self.state.mkdir()
        self.set_ids(uid=0, gid=0, indus_uid=10000, indus_gid=10000)
        (self.state / "indus_groups").write_text("10000\n")
        self.data = self.tmp / "data"
        self.data.mkdir()
        self.log_file = self.tmp / "calls.log"
        self.log_file.touch()

    def write_exe(self, path: Path, body: str) -> None:
        path.write_text(f"#!/bin/sh\n{body.lstrip()}", encoding="utf-8")
        path.chmod(0o755)

    def set_ids(self, **ids: int) -> None:
        for name, value in ids.items():
            (self.state / name).write_text(f"{value}\n")

    def run(
        self, script: str, *args: str, env: Mapping[str, str] | None = None, cwd: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        base = {
            "PATH": f"{self.bin}:/usr/local/bin:/usr/bin:/bin",
            "_INDUS_IMAGE_ROOT": str(self.root),
            "INDUS_HOME": str(self.data),
            "FAKE_STATE": str(self.state),
            "FAKE_LOG": str(self.log_file),
            "HOME": str(self.tmp),
            "LC_ALL": "C",
        }
        base.update(env or {})
        target = self.root / "opt" / "indus" / "docker" / script
        if script in ("run", "finish"):
            target = Path(args[0])
            args = args[1:]
        return subprocess.run(
            [self.shell, str(target), *args],
            env=base,
            cwd=cwd or self.tmp,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def log(self) -> list[str]:
        return self.log_file.read_text(encoding="utf-8").splitlines()

    def calls(self, program: str) -> list[str]:
        return [line for line in self.log() if line.split(" ", 1)[0] == program]


@pytest.fixture(params=SHELLS)
def image(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Image]:
    if not SHELLS:
        pytest.skip("no POSIX shell to run the scripts")
    yield Image(str(request.param), tmp_path)


def chown_targets(img: Image) -> list[Path]:
    return [Path(line.split()[-1]) for line in img.calls("chown")]


# -- dispatcher -----------------------------------------------------------------------------------


def test_pid_1_hands_over_to_s6_init_with_the_wrapper(image: Image) -> None:
    work = image.tmp / "work"
    work.mkdir()
    done = image.run("entrypoint.sh", "gateway", "run", env={"_INDUS_ENTRY_PID": "1"}, cwd=work)
    assert done.returncode == 0, done.stderr
    wrapper = image.root / "opt" / "indus" / "docker" / "main-wrapper.sh"
    # no terminal here, so the services' output is left where s6 puts it by default
    assert image.log() == [f"init {wrapper} gateway run | START={work} LOGGING="]


def test_not_pid_1_warns_runs_stage2_then_the_wrapper(image: Image) -> None:
    done = image.run("entrypoint.sh", "--version", env={"_INDUS_ENTRY_PID": "4242"})
    assert done.returncode == 0, done.stderr
    assert "not this image's" in done.stderr and "services are off" in done.stderr
    assert "indus-prepare: ready" in done.stdout
    assert image.calls("init") == []
    indus = image.calls("indus")
    assert indus[-1].startswith("indus --version |")
    assert image.calls("s6-setuidgid")[-1] == "s6-setuidgid indus indus --version"


# -- stage-2: who may start -----------------------------------------------------------------------


@pytest.mark.parametrize("script", ["stage2.sh", "main-wrapper.sh"])
def test_a_docker_run_user_start_is_refused_with_guidance(image: Image, script: str) -> None:
    image.set_ids(uid=1000, gid=1000)
    done = image.run(script)
    assert done.returncode == 1
    assert "docker run --user" in done.stderr
    assert 'INDUS_UID="$(id -u)"' in done.stderr and "PUID" in done.stderr
    assert image.calls("chown") == [] and image.calls("usermod") == []
    assert image.calls("indus") == []


def test_the_indus_user_itself_skips_the_root_steps(image: Image) -> None:
    image.set_ids(uid=10000, gid=10000)
    (image.data / "config.yaml").write_text("version: 1\n")
    assert image.run("stage2.sh").returncode == 0
    assert image.calls("usermod") == [] and image.calls("chown") == []
    done = image.run("main-wrapper.sh", "chat")
    assert done.returncode == 0, done.stderr
    assert image.calls("s6-setuidgid") == []
    assert image.calls("indus")[-1].startswith("indus chat |")


# -- stage-2: ids and ownership -------------------------------------------------------------------


def test_remap_changes_the_ids_and_only_touches_the_data_folder(image: Image) -> None:
    for name in ("sessions", "logs", "profiles/work"):
        (image.data / name).mkdir(parents=True)
    (image.data / "config.yaml").write_text("version: 1\n")
    (image.data / "host-notes.txt").write_text("the user's own file\n")
    done = image.run("stage2.sh", env={"INDUS_UID": "1234", "INDUS_GID": "2345"})
    assert done.returncode == 0, done.stderr
    assert image.calls("groupmod") == ["groupmod -o -g 2345 indus"]
    assert image.calls("usermod") == ["usermod -o -u 1234 indus"]
    targets = chown_targets(image)
    assert image.data in targets
    for target in targets:
        assert target == image.data or image.data in target.parents, target
    assert image.data / "host-notes.txt" not in targets
    assert all("1234:2345" in line for line in image.calls("chown"))
    # the folder itself is never chowned recursively; our own folders are, without -L
    assert f"chown -h 1234:2345 {image.data}" in image.calls("chown")
    assert f"chown -R -P 1234:2345 {image.data / 'sessions'}" in image.calls("chown")


def test_puid_pgid_are_aliases_and_indus_ids_win(image: Image) -> None:
    (image.data / "config.yaml").write_text("version: 1\n")
    image.run("stage2.sh", env={"PUID": "1500", "PGID": "1600"})
    assert image.calls("usermod") == ["usermod -o -u 1500 indus"]
    assert image.calls("groupmod") == ["groupmod -o -g 1600 indus"]
    image.set_ids(indus_uid=10000, indus_gid=10000)
    image.log_file.write_text("")
    image.run("stage2.sh", env={"PUID": "1500", "INDUS_UID": "1700", "PGID": "1600"})
    assert image.calls("usermod") == ["usermod -o -u 1700 indus"]


@pytest.mark.parametrize("bad", ["abc", "0", "007", "70000", "-5", "1 2"])
def test_invalid_ids_refuse_the_start(image: Image, bad: str) -> None:
    done = image.run("stage2.sh", env={"INDUS_UID": bad})
    assert done.returncode == 1
    assert "must be a number from 1 to 65534" in done.stderr
    assert image.calls("usermod") == []


def test_ownership_already_right_changes_nothing(image: Image) -> None:
    (image.data / "sessions").mkdir()
    (image.data / "sessions" / "s1.json").write_text("{}")
    (image.data / "config.yaml").write_text("version: 1\n")
    if os.getuid() == 0:
        # root here: give the files the default ids for real
        uid = gid = 10000
        for path in (image.data, image.data / "sessions", image.data / "sessions" / "s1.json"):
            os.chown(path, uid, gid)
        os.chown(image.data / "config.yaml", uid, gid)
    else:
        uid, gid = os.getuid(), os.getgid()
    image.set_ids(indus_uid=uid, indus_gid=gid)
    (image.state / "indus_groups").write_text(f"{gid}\n")
    done = image.run("stage2.sh", env={"INDUS_UID": str(uid), "INDUS_GID": str(gid)})
    assert done.returncode == 0, done.stderr
    assert image.calls("chown") == [] and image.calls("usermod") == []


def test_a_symlinked_data_folder_is_refused(image: Image) -> None:
    real = image.tmp / "real-data"
    real.mkdir()
    link = image.tmp / "linked-data"
    link.symlink_to(real)
    done = image.run("stage2.sh", env={"INDUS_HOME": str(link)})
    assert done.returncode == 1 and "symlink" in done.stderr
    nested = image.tmp / "linked-parent"
    nested.symlink_to(image.tmp)
    done = image.run("stage2.sh", env={"INDUS_HOME": str(nested / "data")})
    assert done.returncode == 1 and "symlink" in done.stderr
    assert image.calls("chown") == []


def test_a_symlinked_subfolder_is_never_followed(image: Image) -> None:
    outside = image.tmp / "outside"
    outside.mkdir()
    (outside / "system-file").write_text("not ours\n")
    (image.data / "logs").symlink_to(outside)
    (image.data / ".env").symlink_to(outside / "system-file")
    (image.data / "config.yaml").write_text("version: 1\n")
    done = image.run("stage2.sh")
    assert done.returncode == 0, done.stderr
    assert f"{image.data / 'logs'} is a symlink" in done.stderr
    for target in chown_targets(image):
        assert outside not in (target, *target.parents), target
    assert oct((outside / "system-file").stat().st_mode & 0o777) == oct(0o644)


def test_relative_data_folder_is_refused(image: Image) -> None:
    done = image.run("stage2.sh", env={"INDUS_HOME": "data"})
    assert done.returncode == 1 and "absolute path" in done.stderr


# -- stage-2: .env, first boot, docker socket -----------------------------------------------------


def test_env_file_is_created_and_kept_owner_only(image: Image) -> None:
    (image.data / "config.yaml").write_text("version: 1\n")
    image.run("stage2.sh")
    env_file = image.data / ".env"
    assert env_file.is_file() and env_file.stat().st_mode & 0o777 == 0o600
    env_file.chmod(0o644)
    image.run("stage2.sh")
    assert env_file.stat().st_mode & 0o777 == 0o600


def test_first_boot_seeds_once_as_the_indus_user(image: Image) -> None:
    done = image.run("stage2.sh")
    assert done.returncode == 0, done.stderr
    assert "first start" in done.stdout
    doctor = [c for c in image.calls("indus") if c.startswith("indus doctor --fix |")]
    assert len(doctor) == 1 and f"INDUS_HOME={image.data}" in doctor[0]
    assert any(
        c.startswith("s6-setuidgid indus sh -c") and c.endswith(" indus doctor --fix")
        for c in image.calls("s6-setuidgid")
    )
    assert (image.data / "config.yaml").is_file()
    assert (image.data / "logs" / "container-start.log").is_file()
    image.run("stage2.sh")
    assert len([c for c in image.calls("indus") if "doctor" in c]) == 1


def test_every_start_brings_the_shipped_skills_up_to_date(image: Image) -> None:
    (image.data / "config.yaml").write_text("version: 1\n")
    for _ in range(2):
        assert image.run("stage2.sh").returncode == 0
    syncs = [c for c in image.calls("s6-setuidgid") if c.endswith(" indus skills sync")]
    assert len(syncs) == 2


def test_a_failed_skills_sync_only_warns(image: Image) -> None:
    (image.data / "config.yaml").write_text("version: 1\n")
    image.write_exe(image.bin / "indus", 'echo "indus $*" >> "$FAKE_LOG"; exit 3\n')
    done = image.run("stage2.sh")
    assert done.returncode == 0
    assert "shipped skills were not brought up to date" in done.stderr


def test_browser_variant_points_the_browser_tool_at_chromium(image: Image) -> None:
    (image.root / "opt" / "indus" / ".image_chromium").write_text("/usr/bin/chromium\n")
    assert image.run("stage2.sh").returncode == 0
    sets = [c for c in image.calls("indus") if c.startswith("indus config set")]
    assert sets and sets[0].startswith("indus config set browser.executable_path /usr/bin/chromium")


@pytest.fixture
def docker_socket(image: Image) -> Iterator[Path]:
    run_dir = image.root / "var" / "run"
    run_dir.mkdir(parents=True)
    path = run_dir / "docker.sock"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(str(path))
        yield path
    finally:
        sock.close()


def test_a_docker_socket_gets_a_group_for_indus(image: Image, docker_socket: Path) -> None:
    (image.data / "config.yaml").write_text("version: 1\n")
    gid = docker_socket.stat().st_gid
    done = image.run("stage2.sh")
    assert done.returncode == 0, done.stderr
    assert image.calls("groupadd") == [f"groupadd -g {gid} hostdocker"]
    assert "usermod -aG hostdocker indus" in image.calls("usermod")


def test_an_existing_socket_group_is_reused_and_membership_is_idempotent(
    image: Image, docker_socket: Path
) -> None:
    (image.data / "config.yaml").write_text("version: 1\n")
    gid = docker_socket.stat().st_gid
    (image.state / f"group_{gid}").write_text(f"docker:x:{gid}:\n")
    image.run("stage2.sh")
    assert image.calls("groupadd") == []
    assert "usermod -aG docker indus" in image.calls("usermod")
    image.log_file.write_text("")
    (image.state / "indus_groups").write_text(f"10000 {gid}\n")
    image.run("stage2.sh")
    assert [c for c in image.calls("usermod") if "-aG" in c] == []


# -- the wrapper's routing ------------------------------------------------------------------------


def wrapper_ran(image: Image, *args: str) -> list[str]:
    (image.data / "config.yaml").write_text("version: 1\n")
    done = image.run("main-wrapper.sh", *args)
    assert done.returncode == 0, done.stderr
    return image.calls("s6-setuidgid")


def test_no_arguments_runs_indus(image: Image) -> None:
    assert wrapper_ran(image) == ["s6-setuidgid indus indus"]


@pytest.mark.parametrize(
    "args",
    [("-v",), ("--version",), ("-p", "work", "gateway", "run"), ("-p",)],
)
def test_a_leading_flag_is_always_an_indus_option(image: Image, args: tuple[str, ...]) -> None:
    assert wrapper_ran(image, *args) == ["s6-setuidgid indus indus " + " ".join(args)]


def test_a_program_on_path_is_executed(image: Image) -> None:
    (image.data / "config.yaml").write_text("version: 1\n")
    done = image.run("main-wrapper.sh", "sh", "-c", "echo from-the-container")
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip().endswith("from-the-container")
    assert image.calls("s6-setuidgid") == ["s6-setuidgid indus sh -c echo from-the-container"]
    assert image.calls("indus") == []


@pytest.mark.parametrize("word", ["gateway", "chat", "cd", "run_as_indus"])
def test_anything_else_is_an_indus_subcommand(image: Image, word: str) -> None:
    assert wrapper_ran(image, word, "x") == [f"s6-setuidgid indus indus {word} x"]


def test_the_start_directory_and_home_are_restored(image: Image) -> None:
    work = image.tmp / "project"
    work.mkdir()
    (image.data / "config.yaml").write_text("version: 1\n")
    done = image.run("main-wrapper.sh", "chat", env={"INDUS_START_DIR": str(work)})
    assert done.returncode == 0, done.stderr
    line = image.calls("indus")[-1]
    assert f"PWD={work}" in line and f"HOME={image.data}" in line


# -- s6 services ----------------------------------------------------------------------------------


S6 = DOCKER_DIR / "s6"


@pytest.mark.parametrize(
    ("service", "switch"), [("gateway", "INDUS_GATEWAY"), ("dashboard", "INDUS_DASHBOARD")]
)
def test_services_are_off_unless_switched_on(image: Image, service: str, switch: str) -> None:
    run, finish = S6 / service / "run", S6 / service / "finish"
    off = image.run("run", str(run))
    assert off.returncode == 0 and image.calls("s6-setuidgid") == []
    assert image.run("finish", str(finish)).returncode == 125
    on = {switch: "1"}
    assert image.run("finish", str(finish), env=on).returncode == 0
    assert image.run("run", str(run), env=on).returncode == 0
    call = image.calls("s6-setuidgid")[-1]
    if service == "gateway":
        assert call == "s6-setuidgid indus indus gateway run"
    else:
        assert call == "s6-setuidgid indus indus dashboard --host 127.0.0.1 --port 9119 --no-open"
    assert f"HOME={image.data}" in image.calls("indus")[-1]


def test_shellcheck_is_clean_when_installed() -> None:
    checker = shutil.which("shellcheck")
    if checker is None:
        pytest.skip("shellcheck is not installed (the Docker CI lint lane runs it)")
    files = [DOCKER_DIR / name for name in SCRIPTS]
    files += [S6 / s / f for s in ("gateway", "dashboard") for f in ("run", "finish")]
    done = subprocess.run(
        [checker, "--shell=sh", *map(str, files)], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stdout
