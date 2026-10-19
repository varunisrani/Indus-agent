# pyright: strict
"""Installing, updating and removing user plugins from git or a local folder (P13-21).

Identifiers: ``owner/repo[/sub/dir]`` (GitHub), a git URL (``https://``, ``http://``, ``ssh://``,
``git@host:path``, ``file://``; a custom, unreviewed source), or an existing local folder.

Order of an install, nothing visible until the last step:

1. ``--ref`` must be a full 40-hex commit SHA; the clone is fetched at that commit, checked out
   detached and ``HEAD`` compared with it;
2. everything lands in ``<home>/plugins/.staging-<random>/`` (discovery never reads dot-folders);
   a subdirectory install clones blobless and sparse-checks-out only that folder;
3. the manifest names the plugin; the P13-20 scanner reads the working tree (never ``.git``):
   ``safe`` installs, ``caution`` needs a confirm or ``force``, ``dangerous`` is refused and
   ``force`` never overrides it;
4. one rename publishes ``<home>/plugins/<name>/`` (a replacement moves the old folder aside
   first); then the install record is written to ``<home>/plugins/.install-metadata.json``.

Credentials: each network git command runs anonymously first with every prompt off. When the
remote refuses, it is run once more with a token (``GITHUB_TOKEN`` / ``GH_TOKEN`` for GitHub, then
``gh auth token``, then ``git credential fill``) handed to that one command as an
``http.<origin>/.extraHeader`` through ``GIT_CONFIG_COUNT`` environment entries: never on argv,
never in any git config file.

Update (unpinned git installs): autostash local edits, ``pull --ff-only``, scan, then re-apply the
edits. A ``dangerous`` (or unconfirmed ``caution``) update puts the previous revision back and is
reported, and a dangerous one asks the caller to disable the plugin. Pinned installs refuse
``update``; move a pin with ``install --force --ref <sha>``.
"""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import os
import re
import secrets
import shutil
import signal
import subprocess
import time
import urllib.parse
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from indus.foundation import fsio
from indus.foundation.scope import Scope
from indus.platform.locate import which
from indus.plugins.context import PluginError
from indus.plugins.manifest import PluginManifest, read_manifest
from indus.plugins.settings import bounded_seconds, plugins_setting
from indus.safety.child_env import child_env
from indus.safety.content_scan import ScanReport, scan_tree

__all__ = [
    "BACKUP_DIR",
    "METADATA_FILE",
    "Git",
    "GitResult",
    "InstallBlocked",
    "InstallError",
    "InstallResult",
    "Installer",
    "Source",
    "UpdateResult",
    "UpdateStatus",
    "clone_timeout",
    "forget_plugin",
    "full_sha",
    "parse_source",
    "scrub_url",
]

log = logging.getLogger(__name__)

METADATA_FILE = ".install-metadata.json"
CHECK_FILE = ".update-check.json"
BACKUP_DIR = "plugins-backup"
STAGING_PREFIX = ".staging-"
OLD_PREFIX = ".old-"
DEFAULT_CLONE_TIMEOUT_S = 300.0
MAX_CLONE_TIMEOUT_S = 3600.0
LOCAL_TIMEOUT_S = 60.0
_FULL_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
_GITHUB = re.compile(r"^([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9._-]+)((?:/[^/\s]+)+)?/?$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_URL_SCHEMES = ("https://", "http://", "ssh://", "git://", "file://")
_SCP_LIKE = re.compile(r"^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:.+")
_REFUSED = (
    "authentication failed",
    "could not read username",
    "could not read password",
    "terminal prompts disabled",
    "repository not found",
    "permission denied",
    "access denied",
    "invalid username or password",
    "403",
    "401",
)

Verdict = Literal["safe", "caution", "dangerous"]
Confirm = Callable[[ScanReport], bool]


class InstallError(PluginError):
    default_code = "plugins.install"


class InstallBlocked(InstallError):
    """The scan (or the person) refused the plugin; ``report`` says why."""

    default_code = "plugins.install_blocked"

    def __init__(self, message: str, report: ScanReport | None = None) -> None:
        super().__init__(message)
        self.report = report


# -- identifiers ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Source:
    """Where a plugin comes from. ``url`` is a git URL without credentials, or a local folder."""

    kind: Literal["github", "git", "local"]
    identifier: str
    url: str
    subdir: str = ""

    @property
    def custom(self) -> bool:
        return self.kind == "git"

    def canonical(self) -> str:
        return f"{self.url}#{self.subdir}" if self.subdir else self.url


def scrub_url(url: str) -> str:
    """``url`` without user info, query or fragment (http/https/ssh)."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme in {"http", "https", "ssh", "git"} and parts.hostname:
        host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
        if parts.port is not None:
            host = f"{host}:{parts.port}"
        user = "git@" if parts.scheme == "ssh" and parts.username == "git" else ""
        return urllib.parse.urlunsplit((parts.scheme, user + host, parts.path, "", ""))
    return url


def _clean_subdir(raw: str) -> str:
    parts = [p for p in raw.replace("\\", "/").split("/") if p and p != "."]
    if any(p == ".." for p in parts):
        raise InstallError(f"subdirectory {raw!r} leaves the repository")
    return "/".join(parts)


def parse_source(identifier: str, *, cwd: Path | None = None) -> Source:
    """Read an install identifier (see the module notes)."""
    text = identifier.strip()
    if not text:
        raise InstallError("an install needs a plugin identifier")
    base = cwd if cwd is not None else Path.cwd()
    if text.startswith(("/", "./", "../", "~")) or (len(text) > 1 and text[1] == ":"):
        folder = Path(os.path.expanduser(text))
        folder = folder if folder.is_absolute() else base / folder
        if not folder.is_dir():
            raise InstallError(f"{folder} is not a folder")
        return Source("local", identifier, str(folder.resolve()))
    if text.startswith(_URL_SCHEMES) or _SCP_LIKE.match(text):
        url, _, sub = text.partition("#")
        return Source("git", identifier, scrub_url(url), _clean_subdir(sub))
    match = _GITHUB.match(text)
    if match is not None:
        owner, repo, sub = match.group(1), match.group(2), match.group(3) or ""
        repo = repo.removesuffix(".git")
        return Source(
            "github", identifier, f"https://github.com/{owner}/{repo}.git", _clean_subdir(sub)
        )
    folder = base / text
    if folder.is_dir():
        return Source("local", identifier, str(folder.resolve()))
    raise InstallError(
        f"{identifier!r} is not owner/repo, a git URL or a local folder",
        hint="examples: someone/indus-plugin-x, https://host/repo.git, ./my-plugin",
    )


def full_sha(ref: str) -> str:
    """A full 40-hex commit SHA, lowercased; anything else (tags, branches, short SHAs) refused."""
    if not _FULL_SHA.match(ref.strip()):
        raise InstallError(
            f"--ref must be a full 40-character commit SHA, not {ref!r}",
            hint="tags, branch names and short SHAs can move; pin the exact commit",
        )
    return ref.strip().lower()


def clone_timeout(scope: Scope | None) -> float:
    """``plugins.clone_timeout_seconds`` (300 s; at most 3,600; 0 falls back to the default)."""
    value = bounded_seconds(
        plugins_setting(scope, "clone_timeout_seconds", None),
        name="plugins.clone_timeout_seconds",
        default=DEFAULT_CLONE_TIMEOUT_S,
        maximum=MAX_CLONE_TIMEOUT_S,
    )
    return value if value > 0 else DEFAULT_CLONE_TIMEOUT_S


# -- running git ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class GitResult:
    code: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.code == 0


Runner = Callable[[Sequence[str], Path, Mapping[str, str], float, str | None], GitResult]


def _kill_group(proc: subprocess.Popen[str]) -> None:
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        with contextlib.suppress(OSError):
            proc.kill()
    with contextlib.suppress(subprocess.TimeoutExpired, OSError):
        proc.wait(timeout=5)


def run_process(
    argv: Sequence[str], cwd: Path, env: Mapping[str, str], timeout: float, stdin: str | None
) -> GitResult:
    """Run ``argv`` in its own process group; the group is killed on a timeout or any error."""
    try:
        proc = subprocess.Popen(
            list(argv),
            cwd=str(cwd),
            env=dict(env),
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=os.name == "posix",
        )
    except FileNotFoundError as exc:
        raise InstallError(f"{argv[0]} is not installed or not on PATH") from exc
    except OSError as exc:
        raise InstallError(f"could not run {argv[0]}: {exc}") from exc
    try:
        out, err = proc.communicate(input=stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        raise InstallError(
            f"`{' '.join(argv[:2])}` did not finish within {timeout:g} s and was stopped",
            hint="raise plugins.clone_timeout_seconds for slow connections",
        ) from None
    except BaseException:
        _kill_group(proc)
        raise
    return GitResult(proc.returncode, out or "", err or "")


def _refused(result: GitResult) -> bool:
    text = result.stderr.lower()
    return any(word in text for word in _REFUSED)


def _origin(url: str) -> str | None:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in {"https", "http"} or not parts.hostname:
        return None
    netloc = parts.hostname + (f":{parts.port}" if parts.port else "")
    return f"{parts.scheme}://{netloc}/"


class Git:
    """Non-interactive git for one profile, with the anonymous-then-token fallback."""

    def __init__(
        self,
        scope: Scope,
        *,
        timeout_s: float | None = None,
        runner: Runner | None = None,
        executable: str | None = None,
    ) -> None:
        self.scope = scope
        self.timeout_s = timeout_s if timeout_s is not None else clone_timeout(scope)
        self.runner: Runner = runner if runner is not None else run_process
        self._exe = executable

    def executable(self) -> str:
        found = self._exe or which("git", self.scope.env.get("PATH"))
        if not found:
            raise InstallError("git is not installed or not on PATH")
        return str(found)

    def env(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        env = child_env(self.scope)
        env.update(
            {
                "GIT_TERMINAL_PROMPT": "0",
                "GCM_INTERACTIVE": "never",
                "GIT_ASKPASS": "",
                "SSH_ASKPASS": "",
                "GIT_SSH_COMMAND": env.get("GIT_SSH_COMMAND") or "ssh -o BatchMode=yes",
            }
        )
        env.update(extra or {})
        return env

    def run(
        self,
        cwd: Path,
        *args: str,
        remote: str | None = None,
        timeout: float | None = None,
        stdin: str | None = None,
    ) -> GitResult:
        """``git args`` in ``cwd``. ``remote`` marks a network command: when the remote refuses the
        anonymous try, it runs once more with a one-shot credential header."""
        argv = [self.executable(), *args]
        limit = timeout if timeout is not None else (self.timeout_s if remote else LOCAL_TIMEOUT_S)
        result = self.runner(argv, cwd, self.env(), limit, stdin)
        if result.ok or remote is None or not _refused(result):
            return result
        header = self.auth_header(remote)
        if header is None:
            return result
        origin = _origin(remote)
        if origin is None:
            return result
        log.info(
            "the remote refused anonymous access; retrying %s with a stored credential", args[0]
        )
        return self.runner(argv, cwd, self.env(self._one_shot(header, origin)), limit, stdin)

    def _one_shot(self, header: str, origin: str) -> dict[str, str]:
        """``GIT_CONFIG_*`` entries adding the header after any the environment already has."""
        try:
            count = max(0, int(self.env().get("GIT_CONFIG_COUNT", "0") or "0"))
        except ValueError:
            count = 0
        return {
            "GIT_CONFIG_COUNT": str(count + 1),
            f"GIT_CONFIG_KEY_{count}": f"http.{origin}.extraHeader",
            f"GIT_CONFIG_VALUE_{count}": header,
        }

    # -- credentials ----------------------------------------------------------------------------

    def _secret(self, name: str) -> str | None:
        value = self.scope.env.get(name)
        if value:
            return value
        store = self.scope.secrets
        if store is None:
            return None
        try:
            return store.get(name)
        except Exception:
            log.debug("secret %s could not be read", name, exc_info=True)
            return None

    def _gh_token(self) -> str | None:
        gh = which("gh", self.scope.env.get("PATH"))
        if not gh:
            return None
        try:
            result = self.runner(
                [str(gh), "auth", "token", "--hostname", "github.com"],
                self.scope.home,
                self.env(),
                15.0,
                None,
            )
        except InstallError:
            return None
        token = result.stdout.strip()
        return token if result.ok and token else None

    def _helper_credential(self, url: str) -> tuple[str, str] | None:
        parts = urllib.parse.urlsplit(url)
        if not parts.hostname:
            return None
        host = parts.hostname + (f":{parts.port}" if parts.port else "")
        query = f"protocol={parts.scheme}\nhost={host}\npath={parts.path.lstrip('/')}\n\n"
        try:
            result = self.runner(
                [self.executable(), "credential", "fill"], self.scope.home, self.env(), 15.0, query
            )
        except InstallError:
            return None
        if not result.ok:
            return None
        found: dict[str, str] = {}
        for line in result.stdout.splitlines():
            key, sep, value = line.partition("=")
            if sep:
                found[key] = value
        user, password = found.get("username"), found.get("password")
        return (user, password) if user and password else None

    def auth_header(self, url: str) -> str | None:
        """An ``Authorization`` header for ``url``'s host, or None when no credential is known."""
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in {"https", "http"} or not parts.hostname:
            return None
        pair: tuple[str, str] | None = None
        if parts.hostname.lower() == "github.com":
            token = self._secret("GITHUB_TOKEN") or self._secret("GH_TOKEN") or self._gh_token()
            if token:
                pair = ("x-access-token", token)
        if pair is None:
            pair = self._helper_credential(url)
        if pair is None:
            return None
        raw = base64.b64encode(f"{pair[0]}:{pair[1]}".encode()).decode("ascii")
        return f"Authorization: Basic {raw}"

    def check(self, cwd: Path, *args: str, remote: str | None = None, what: str = "") -> GitResult:
        result = self.run(cwd, *args, remote=remote)
        if not result.ok:
            detail = (result.stderr or result.stdout).strip()[-800:]
            if remote:
                detail = detail.replace(remote, scrub_url(remote))
            raise InstallError(f"{what or 'git ' + args[0]} failed: {detail or 'no output'}")
        return result

    def head(self, repo: Path) -> str:
        return self.check(repo, "rev-parse", "HEAD").stdout.strip().lower()


# -- metadata -------------------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise InstallError(f"{path} cannot be read ({exc}); fix or remove it first") from exc
    if not isinstance(data, Mapping):
        raise InstallError(f"{path} must hold a JSON object; fix or remove it first")
    return dict(cast(Mapping[str, Any], data))


# -- results --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class InstallResult:
    name: str
    target: Path
    record: Mapping[str, Any]
    report: ScanReport
    manifest: PluginManifest | None
    replaced: bool = False
    backup: Path | None = None


@dataclass(frozen=True)
class UpdateResult:
    name: str
    old: str
    new: str
    changed: bool
    report: ScanReport | None = None
    blocked: str = ""
    disable: bool = False
    note: str = ""


@dataclass(frozen=True)
class UpdateStatus:
    name: str
    current: str
    latest: str = ""
    available: bool = False
    note: str = ""


# -- the installer --------------------------------------------------------------------------------


def _manifest_name(tree: Path, fallback: str) -> tuple[str, PluginManifest | None]:
    read = read_manifest(tree, source="user")
    manifest = read.manifest if read is not None else None
    name = manifest.name if manifest is not None else fallback
    if not _NAME.match(name):
        raise InstallError(
            f"plugin name {name!r} is not a usable folder name (letters, digits, '.', '_', '-')"
        )
    return name, manifest


def _kept_pin(records: Mapping[str, Mapping[str, Any]], source: Source) -> str | None:
    """The pin of an earlier pinned install of the same source: a reinstall without ``--ref``
    stays on it (moving a pin always takes an explicit ``--ref``)."""
    pins = {
        str(r.get("revision") or "")
        for r in records.values()
        if r.get("source") == source.canonical() and r.get("pinned") is True
    }
    found = next(iter(pins)) if len(pins) == 1 else ""
    return found if _FULL_SHA.match(found) else None


def _repo_name(url: str) -> str:
    tail = url.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    return tail.removesuffix(".git") or "plugin"


@dataclass
class _Staged:
    folder: Path
    tree: Path
    revision: str = ""
    repo: Path | None = None


@dataclass
class Installer:
    """Install, update, check and remove plugins under ``<home>/plugins/``."""

    scope: Scope
    git: Git | None = None
    _git: Git = field(init=False)

    def __post_init__(self) -> None:
        self._git = self.git if self.git is not None else Git(self.scope)

    @property
    def plugins_dir(self) -> Path:
        return self.scope.home / "plugins"

    @property
    def metadata_path(self) -> Path:
        return self.plugins_dir / METADATA_FILE

    def _lock(self) -> fsio.FileLock:
        self.plugins_dir.mkdir(parents=True, exist_ok=True)
        return fsio.FileLock(self.plugins_dir / ".install.lock", timeout=60.0)

    # -- records --------------------------------------------------------------------------------

    def records(self) -> dict[str, dict[str, Any]]:
        data = _read_json(self.metadata_path)
        return {
            k: dict(cast(Mapping[str, Any], v)) for k, v in data.items() if isinstance(v, Mapping)
        }

    def record(self, name: str) -> dict[str, Any] | None:
        return self.records().get(name)

    def _set_record(self, name: str, record: Mapping[str, Any] | None) -> None:
        data = _read_json(self.metadata_path)
        if record is None:
            data.pop(name, None)
        else:
            data[name] = dict(record)
        fsio.atomic_write_json(self.metadata_path, data, sort_keys=True, mode=0o600)

    # -- staging --------------------------------------------------------------------------------

    def _sweep(self) -> None:
        """Remove dot-folders an earlier, interrupted install left behind (lock held)."""
        for child in self.plugins_dir.iterdir():
            if child.is_dir() and child.name.startswith((STAGING_PREFIX, OLD_PREFIX)):
                shutil.rmtree(child, ignore_errors=True)

    def _fetch(self, source: Source, pin: str | None, folder: Path) -> _Staged:
        repo = folder / "repo"
        if source.kind == "local":
            shutil.copytree(
                source.url,
                repo,
                symlinks=True,
                ignore=shutil.ignore_patterns(".git", "__pycache__"),
            )
            return _Staged(folder, repo)
        git = self._git
        args = ["clone", "--depth", "1", "--no-checkout"]
        if source.subdir:
            args.append("--filter=blob:none")
        git.check(folder, *args, source.url, str(repo), remote=source.url, what="git clone")
        if source.subdir:
            git.check(repo, "sparse-checkout", "set", source.subdir, what="sparse checkout")
        if pin is not None:
            git.check(repo, "fetch", "--depth", "1", "origin", pin, remote=source.url, what="fetch")
            git.check(repo, "checkout", "--detach", pin, remote=source.url, what="checkout")
            actual = git.head(repo)
            if actual != pin:
                raise InstallError(
                    f"the checkout is at {actual}, not the pinned commit {pin}; nothing installed"
                )
        else:
            git.check(repo, "checkout", remote=source.url, what="checkout")
        git.check(repo, "remote", "set-url", "origin", scrub_url(source.url))
        tree = repo
        if source.subdir:
            tree = (repo / source.subdir).resolve()
            if not tree.is_relative_to(repo.resolve()) or not tree.is_dir():
                raise InstallError(f"{source.subdir} is not a folder in {source.url}")
        return _Staged(folder, tree, git.head(repo), repo)

    def scan(self, tree: Path, manifest: PluginManifest | None, name: str) -> ScanReport:
        """The P13-20 scan of a plugin folder (the scanner never enters ``.git``)."""
        allowed = manifest.env_names() if manifest is not None else []
        return scan_tree(tree, kind="plugin", allowed_env=allowed, name=name)

    @staticmethod
    def judge(report: ScanReport, *, force: bool, confirm: Confirm | None, what: str) -> str | None:
        """Why ``report`` stops ``what`` (None: go ahead)."""
        if report.verdict == "dangerous":
            return f"{what} blocked: the scan found dangerous content ({report.summary()})"
        if report.verdict == "caution" and not force:
            agreed = False
            if confirm is not None:
                try:
                    agreed = bool(confirm(report))
                except Exception:
                    log.warning("the scan confirmation failed; treated as no", exc_info=True)
            if not agreed:
                return (
                    f"{what} stopped: the scan asks for caution ({report.summary()}); confirm on "
                    "a terminal or pass --force"
                )
        return None

    def _local_edits(self, target: Path) -> list[str]:
        if not (target / ".git").is_dir():
            return []
        status = self._git.run(target, "status", "--porcelain", "--untracked-files=no")
        if not status.ok:
            return []
        return [line[3:].strip() for line in status.stdout.splitlines() if line[:2].strip()]

    def _backup(self, name: str, target: Path, revision: str, files: Sequence[str]) -> Path | None:
        if not files:
            return None
        backup = self.scope.home / BACKUP_DIR / f"{name}-{(revision or 'local')[:7]}"
        for rel in files:
            source = target / rel
            if source.is_file():
                dest = backup / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, dest)
        return backup

    def _publish(self, tree: Path, target: Path) -> None:
        if target.exists():
            old = self.plugins_dir / f"{OLD_PREFIX}{secrets.token_hex(6)}"
            target.rename(old)
            try:
                tree.rename(target)
            except BaseException:
                old.rename(target)
                raise
            shutil.rmtree(old, ignore_errors=True)
        else:
            tree.rename(target)

    # -- install --------------------------------------------------------------------------------

    def install(
        self,
        identifier: str,
        *,
        ref: str | None = None,
        force: bool = False,
        confirm: Confirm | None = None,
        cwd: Path | None = None,
    ) -> InstallResult:
        pin = full_sha(ref) if ref is not None else None
        source = parse_source(identifier, cwd=cwd)
        if pin is not None and source.kind == "local":
            raise InstallError("--ref applies to git sources only")
        with self._lock():
            records = self.records()
            if pin is None:
                pin = _kept_pin(records, source)
            self._sweep()
            folder = self.plugins_dir / f"{STAGING_PREFIX}{secrets.token_hex(6)}"
            folder.mkdir(parents=True)
            try:
                return self._install_staged(source, pin, folder, records, force, confirm)
            finally:
                shutil.rmtree(folder, ignore_errors=True)

    def _install_staged(
        self,
        source: Source,
        pin: str | None,
        folder: Path,
        records: Mapping[str, Mapping[str, Any]],
        force: bool,
        confirm: Confirm | None,
    ) -> InstallResult:
        staged = self._fetch(source, pin, folder)
        fallback = source.subdir.rsplit("/", 1)[-1] if source.subdir else _repo_name(source.url)
        name, manifest = _manifest_name(staged.tree, fallback)
        target = self.plugins_dir / name
        if target.is_symlink():
            raise InstallError(f"{target} is a link; remove it before installing {name}")
        prior = records.get(name)
        if target.exists() and not force:
            raise InstallError(
                f"plugin {name} is already installed",
                hint=f"run `indus plugins update {name}`, or reinstall with --force",
            )
        if target.exists() and pin is None and prior is not None and prior.get("pinned") is True:
            raise InstallError(
                f"plugin {name} is pinned to {prior.get('revision')}",
                hint="reinstall with --force --ref <40-character commit SHA> to move the pin",
            )
        report = self.scan(staged.tree, manifest, name)
        refusal = self.judge(report, force=force, confirm=confirm, what=f"install of {name}")
        if refusal is not None:
            raise InstallBlocked(refusal, report)
        backup: Path | None = None
        replaced = target.exists()
        if replaced:
            old_rev = str(prior.get("revision") or "") if prior is not None else ""
            backup = self._backup(name, target, old_rev, self._local_edits(target))
        record: dict[str, Any] = {
            "source": source.canonical(),
            "kind": source.kind,
            "identifier": source.identifier,
            "subdir": source.subdir,
            "revision": staged.revision,
            "pinned": pin is not None,
            "installed_at": _now_iso(),
            "update_url": manifest.update_url if manifest is not None else "",
            "scan": {"scanner": report.scanner, "verdict": report.verdict},
        }
        self._publish(staged.tree, target)
        self._set_record(name, record)
        log.info("plugin %s installed from %s at %s", name, source.canonical(), staged.revision)
        return InstallResult(name, target, record, report, manifest, replaced, backup)

    # -- update ---------------------------------------------------------------------------------

    def update(
        self,
        name: str,
        *,
        force: bool = False,
        confirm: Confirm | None = None,
        trust_update_url: bool = False,
    ) -> UpdateResult:
        record = self.record(name)
        target = self.plugins_dir / name
        if record is None or not target.is_dir():
            raise InstallError(f"plugin {name} was not installed with `indus plugins install`")
        if record.get("pinned") is True:
            raise InstallError(
                f"plugin {name} is pinned to {record.get('revision')}; pinned plugins are not "
                "updated",
                hint="move the pin with `indus plugins install --force --ref <sha>`",
            )
        if record.get("kind") == "local":
            raise InstallError(
                f"plugin {name} was copied from a folder; reinstall it with --force to refresh it"
            )
        if record.get("subdir") or not (target / ".git").is_dir():
            return self._update_by_reinstall(name, record, force, confirm, trust_update_url)
        with self._lock():
            return self._update_in_place(name, record, target, force, confirm, trust_update_url)

    def _update_by_reinstall(
        self,
        name: str,
        record: Mapping[str, Any],
        force: bool,
        confirm: Confirm | None,
        trust_update_url: bool,
    ) -> UpdateResult:
        old = str(record.get("revision") or "")
        target = self.plugins_dir / name
        saved = self.scope.home / BACKUP_DIR / f"{name}-{(old or 'local')[:7]}"
        shutil.rmtree(saved, ignore_errors=True)
        shutil.copytree(target, saved, symlinks=True)
        try:
            result = self.install(
                str(record.get("identifier") or ""),
                force=True,
                confirm=confirm if not force else None,
            )
        except InstallBlocked as exc:
            disable = exc.report is not None and exc.report.verdict == "dangerous"
            return UpdateResult(name, old, old, False, exc.report, str(exc), disable)
        new = str(result.record.get("revision") or "")
        note = f"the previous folder is kept in {saved}"
        return UpdateResult(name, old, new, new != old, result.report, note=note)

    def _restore(self, target: Path, old: str, stash: str) -> str:
        self._git.run(target, "reset", "--hard", old)
        return self._reapply(target, stash)

    def _reapply(self, target: Path, stash: str) -> str:
        if not stash:
            return ""
        applied = self._git.run(target, "stash", "apply", stash)
        conflicted = self._git.run(target, "diff", "--name-only", "--diff-filter=U").stdout.strip()
        if applied.ok and not conflicted:
            top = self._git.run(target, "rev-parse", "--verify", "-q", "refs/stash").stdout.strip()
            if top == stash:
                self._git.run(target, "stash", "drop")
            return "your local edits were re-applied"
        self._git.run(target, "reset", "--hard", "HEAD")
        return (
            f"your local edits conflict with the update; they are kept in git stash ({stash[:12]}) "
            f"inside {target}"
        )

    def _update_in_place(
        self,
        name: str,
        record: Mapping[str, Any],
        target: Path,
        force: bool,
        confirm: Confirm | None,
        trust_update_url: bool,
    ) -> UpdateResult:
        git = self._git
        url = str(record.get("source") or "").partition("#")[0]
        old = git.head(target)
        stash = ""
        if git.run(target, "status", "--porcelain").stdout.strip():
            before = git.run(target, "rev-parse", "--verify", "-q", "refs/stash").stdout.strip()
            git.run(target, "stash", "push", "--include-untracked", "-m", "indus plugin update")
            after = git.run(target, "rev-parse", "--verify", "-q", "refs/stash").stdout.strip()
            if not after or after == before:
                raise InstallError(
                    f"local edits in {target} could not be set aside; nothing changed"
                )
            stash = after
        pulled = git.run(target, "pull", "--ff-only", remote=url or None)
        if not pulled.ok:
            note = self._reapply(target, stash)
            detail = (pulled.stderr or pulled.stdout).strip()[-500:]
            raise InstallError(
                f"update of {name} failed: {detail}" + (f" ({note})" if note else "")
            )
        new = git.head(target)
        if new == old:
            return UpdateResult(name, old, new, False, note=self._reapply(target, stash))
        read = read_manifest(target, source="user")
        manifest = read.manifest if read is not None else None
        url_now = manifest.update_url if manifest is not None else ""
        url_before = str(record.get("update_url") or "")
        if url_now != url_before and not trust_update_url:
            note = self._restore(target, old, stash)
            why = (
                f"update of {name} stopped: its update_url changed from {url_before or '(none)'} "
                f"to {url_now or '(none)'}; rerun with --trust-update-url if you trust it"
            )
            return UpdateResult(name, old, old, False, None, why, note=note)
        report = self.scan(target, manifest, name)
        refusal = self.judge(report, force=force, confirm=confirm, what=f"update of {name}")
        if refusal is not None:
            note = self._restore(target, old, stash)
            return UpdateResult(
                name, old, old, False, report, refusal, report.verdict == "dangerous", note
            )
        note = self._reapply(target, stash)
        updated = dict(record)
        updated.update(
            revision=new,
            installed_at=_now_iso(),
            update_url=url_now,
            scan={"scanner": report.scanner, "verdict": report.verdict},
        )
        self._set_record(name, updated)
        return UpdateResult(name, old, new, True, report, note=note)

    # -- update checks --------------------------------------------------------------------------

    def check_updates(self, *, now: float | None = None) -> list[UpdateStatus]:
        """Compare each git install's revision with its origin's ``HEAD`` (network); the result
        and ``now`` (default: the wall clock) are saved for ``list`` and the periodic check."""
        statuses: list[UpdateStatus] = []
        for name, record in sorted(self.records().items()):
            current = str(record.get("revision") or "")
            if record.get("kind") == "local":
                statuses.append(UpdateStatus(name, current, note="local copy"))
                continue
            if record.get("pinned") is True:
                statuses.append(UpdateStatus(name, current, note="pinned"))
                continue
            url = str(record.get("source") or "").partition("#")[0]
            try:
                result = self._git.run(self.scope.home, "ls-remote", url, "HEAD", remote=url)
            except InstallError as exc:
                statuses.append(UpdateStatus(name, current, note=str(exc)))
                continue
            latest = result.stdout.split()[0].lower() if result.ok and result.stdout.split() else ""
            if not latest:
                statuses.append(UpdateStatus(name, current, note="the origin could not be read"))
                continue
            statuses.append(UpdateStatus(name, current, latest, latest != current))
        self._save_check(statuses, now if now is not None else time.time())
        return statuses

    def _save_check(self, statuses: Sequence[UpdateStatus], at: float) -> None:
        data = {
            "checked_at": at,
            "available": {s.name: s.latest for s in statuses if s.available},
        }
        self.plugins_dir.mkdir(parents=True, exist_ok=True)
        fsio.atomic_write_json(self.plugins_dir / CHECK_FILE, data, sort_keys=True)

    def last_check(self) -> Mapping[str, Any]:
        try:
            return _read_json(self.plugins_dir / CHECK_FILE)
        except InstallError:
            return {}

    def maybe_check_updates(self, *, now: float | None = None) -> list[UpdateResult] | None:
        """The periodic check: None when not due (``plugins.auto_update_check_hours``; 0 = off);
        otherwise checks and, only with ``plugins.auto_apply``, applies what it found (unattended:
        a caution scan never passes)."""
        hours = plugins_setting(self.scope, "auto_update_check_hours", 24.0)
        try:
            interval = float(hours) * 3600.0
        except (TypeError, ValueError):
            interval = 24 * 3600.0
        if interval <= 0:
            return None
        moment = now if now is not None else time.time()
        last = self.last_check().get("checked_at")
        if isinstance(last, int | float) and moment - float(last) < interval:
            return None
        statuses = self.check_updates(now=moment)
        if plugins_setting(self.scope, "auto_apply", False) is not True:
            return []
        applied: list[UpdateResult] = []
        for status in statuses:
            if status.available:
                try:
                    applied.append(self.update(status.name))
                except InstallError as exc:
                    log.warning("automatic update of plugin %s failed: %s", status.name, exc)
        return applied

    # -- remove ---------------------------------------------------------------------------------

    def remove(self, name: str) -> bool:
        """Delete the plugin folder and its record; False when there was nothing to remove."""
        if not _NAME.match(name):
            raise InstallError(f"{name!r} is not a plugin name")
        target = self.plugins_dir / name
        with self._lock():
            existed = target.exists() or target.is_symlink()
            if target.is_symlink() or target.is_file():
                target.unlink()
            elif target.is_dir():
                shutil.rmtree(target)
            had_record = name in self.records()
            if had_record:
                self._set_record(name, None)
        return existed or had_record


def forget_plugin(tree: MutableMapping[str, Any], name: str) -> list[str]:
    """Drop every config trace of plugin ``name`` from a config tree (in place); what changed."""
    changed: list[str] = []
    section = tree.get("plugins")
    if isinstance(section, MutableMapping):
        plugins = cast(MutableMapping[str, Any], section)
        for key in ("enabled", "disabled"):
            names = plugins.get(key)
            if isinstance(names, list) and name in names:
                plugins[key] = [n for n in cast(list[Any], names) if n != name]
                changed.append(f"plugins.{key}")
        entries = plugins.get("entries")
        if isinstance(entries, MutableMapping) and name in entries:
            del cast(MutableMapping[str, Any], entries)[name]
            changed.append(f"plugins.entries.{name}")
    memory = tree.get("memory")
    if isinstance(memory, MutableMapping):
        block = cast(MutableMapping[str, Any], memory)
        if block.get("provider") == name:
            block["provider"] = ""
            changed.append("memory.provider")
    return changed
