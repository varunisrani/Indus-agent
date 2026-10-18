# pyright: strict
"""Dangerous-command detection: ``detect(command) -> Detection`` (P05-07, D-16.76).

The verdict is reached on a parsed command, not by matching a list of regular expressions
against the raw text:

1. limits (size, separators) and hidden unicode controls are checked on the text as written;
2. the text is normalised (ANSI and NUL removed, NFKC, line continuations joined, ``$IFS``
   collapsed, simple ``${name:-default}`` / ``${name/a/b}`` expansions and literal
   ``$(echo word)`` substitutions resolved);
3. ``shlex_lite`` splits it into simple commands; unterminated quoting fails closed;
4. every simple command is unwrapped (``sudo``, ``env``, ``nice``, ``timeout`` … with their option
   arities), its paths have the user and Indus homes folded to ``~`` / ``$INDUS_HOME``, and the
   rules for its program run; payloads (``sh -c``, ``eval``, interpreter ``-c``/``-e``, ``env -S``,
   ``find -exec``, ``xargs``, command and process substitutions, here-documents fed to a shell)
   are analysed the same way, up to ``MAX_DEPTH`` levels;
5. pipelines are checked as a whole (download or decode piped into a shell, a secret file piped
   into an upload).

Hardline findings (never run, whatever the approval mode) stop the analysis at once; otherwise the
first dangerous finding is reported. ``trace`` records how the verdict was reached.
"""

from __future__ import annotations

import re
import shlex
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from indus.foundation.scope import scope_or_none
from indus.safety.approvals.shlex_lite import (
    ShellLimitError,
    ShellSyntaxError,
    SimpleCommand,
    Word,
    parse,
    pipelines,
)

MAX_CHARS = 100_000
MAX_SEPARATORS = 5_000
MAX_UNBROKEN = 16_384
MAX_WORDS = 20_000
MAX_DEPTH = 6


@dataclass(frozen=True)
class Detection:
    dangerous: bool
    hardline: bool
    pattern_key: str | None
    description: str | None
    trace: tuple[str, ...] = ()


SAFE = Detection(False, False, None, None, ())

HARDLINE: dict[str, str] = {
    "rm_root": "recursive delete of the filesystem root",
    "rm_system": "recursive delete of a system directory",
    "rm_home": "recursive delete of the home directory",
    "mkfs": "creates a filesystem (formats a disk)",
    "block_device_write": "writes straight to a raw block device",
    "fork_bomb": "fork bomb",
    "kill_all": "signals every process on the machine",
    "power": "shuts down, halts or reboots the machine",
    "parser_limit": "command too large or too deeply nested to check",
    "malformed": "quoting or substitution that never closes",
}
DANGEROUS: dict[str, str] = {
    "rm_recursive": "recursive delete outside the working tree",
    "rm_protected": "deletes a system or credential file",
    "find_delete": "find that deletes what it matches",
    "xargs_delete": "xargs feeding a delete command",
    "dynamic_word": "unquoted glob or brace word that can expand into a delete or exec option",
    "chmod_world_writable": "makes files writable by every user",
    "chown_root_recursive": "recursively hands files to root",
    "chmod_exec_run": "marks a file executable and runs it straight away",
    "disk_tool": "partitions, wipes or copies a raw disk",
    "sql_destructive": "SQL that drops or empties data",
    "system_config_write": "overwrites or edits system configuration",
    "user_secret_write": "overwrites SSH, shell start-up or credential files",
    "indus_config_write": "overwrites Indus's own config, env or credential files",
    "project_env_write": "overwrites the project's .env or config.yaml",
    "service_change": "stops, restarts or disables a system service",
    "kill_force": "force-kills processes",
    "kill_self": "kills Indus itself",
    "remote_exec": "runs code downloaded from the network",
    "decode_exec": "decodes hidden text and runs it",
    "metadata_endpoint": "reads the cloud instance metadata endpoint (credentials)",
    "secret_upload": "uploads a secret or credential file",
    "hidden_unicode": "invisible or bidirectional unicode control characters",
    "heredoc_exec": "feeds a here-document to a shell or interpreter",
    "git_destructive": "discards work or rewrites shared git history",
    "package_remove": "removes installed packages",
    "docker_remote": "points docker or podman at another daemon",
    "docker_lifecycle": "stops, kills or restarts containers",
    "sudo_privilege": "sudo with a password-on-stdin, askpass, shell or listing flag",
    "indus_lifecycle": "stops, restarts, updates or backgrounds the Indus gateway",
}
DESCRIPTIONS: dict[str, str] = {**HARDLINE, **DANGEROUS}

# -- word lists ----------------------------------------------------------------------------------

SHELLS = frozenset({"sh", "bash", "zsh", "ksh", "dash", "ash", "mksh", "fish", "rbash", "yash"})
_PYTHON = re.compile(r"^(?:python(?:[23](?:\.\d+)?)?|py|pypy3?)$")
INTERPRETERS: dict[str, frozenset[str]] = {
    "node": frozenset({"node", "nodejs"}),
    "perl": frozenset({"perl", "perl5"}),
    "ruby": frozenset({"ruby"}),
    "php": frozenset({"php"}),
    "bun": frozenset({"bun"}),
    "deno": frozenset({"deno"}),
}
_EXEC_FLAGS: dict[str, tuple[str, ...]] = {
    "python": ("-c",),
    "node": ("-e", "--eval", "-p", "--print"),
    "perl": ("-e", "-E"),
    "ruby": ("-e",),
    "php": ("-r",),
    "bun": ("-e", "--eval", "-p", "--print"),
    "deno": ("-e", "--eval", "eval"),
}
_EXEC_LETTER = {"python": "c", "node": "e", "perl": "e", "ruby": "e", "php": "r", "bun": "e"}
_INTERP_ARG_OPTS: dict[str, frozenset[str]] = {
    "python": frozenset({"-W", "-X"}),
    "node": frozenset({"-r", "--require", "--import", "--loader", "-C", "--conditions", "--title"}),
    "perl": frozenset({"-I", "-M", "-m"}),
    "ruby": frozenset({"-I", "-r", "-C", "-E", "-F"}),
    "php": frozenset({"-c", "-d", "-z"}),
    "bun": frozenset({"--cwd", "--config", "--preload", "--env-file"}),
    "deno": frozenset({"-L", "--log-level"}),
}
FETCHERS = frozenset({"curl", "wget", "fetch", "http", "https", "xh", "aria2c", "lwp-request"})
DELETERS = frozenset({"rm", "shred", "unlink", "rmdir", "srm"})
SQL_CLIENTS = frozenset(
    {"psql", "mysql", "mariadb", "sqlite3", "sqlite", "duckdb", "sqlcmd", "clickhouse-client"}
)
SQL_VERBS = frozenset({"drop", "delete", "truncate"})
KEYWORDS = frozenset({"if", "then", "else", "elif", "do", "while", "until", "!", "{"})
CLOSERS = frozenset({"}", "fi", "done", "esac"})
METADATA_HOSTS = (
    "169.254.169.254",
    "169.254.170.2",
    "metadata.google.internal",
    "metadata.goog",
    "100.100.100.200",
    "fd00:ec2::254",
)
SYSTEM_DIRS = frozenset(
    [
        "/applications",
        "/bin",
        "/boot",
        "/dev",
        "/etc",
        "/home",
        "/lib",
        "/lib32",
        "/lib64",
        "/library",
        "/media",
        "/mnt",
        "/nix",
        "/opt",
        "/private",
        "/private/etc",
        "/private/var",
        "/proc",
        "/root",
        "/run",
        "/sbin",
        "/snap",
        "/srv",
        "/sys",
        "/system",
        "/users",
        "/usr",
        "/usr/bin",
        "/usr/lib",
        "/usr/local",
        "/usr/sbin",
        "/usr/share",
        "/var",
        "/var/lib",
        "/var/log",
    ]
)
SYSTEM_CONFIG_PREFIXES = tuple(
    [
        "/etc/",
        "/private/etc/",
        "/usr/",
        "/boot/",
        "/bin/",
        "/sbin/",
        "/lib/",
        "/lib64/",
        "/system/",
        "/library/",
        "/var/spool/cron",
    ]
)
SHELL_RC = frozenset(
    [
        ".bashrc",
        ".bash_profile",
        ".bash_login",
        ".bash_logout",
        ".profile",
        ".zshrc",
        ".zprofile",
        ".zshenv",
        ".zlogin",
        ".kshrc",
    ]
)
HOME_CREDENTIALS = frozenset(
    [
        "~/.netrc",
        "~/.pgpass",
        "~/.npmrc",
        "~/.pypirc",
        "~/.git-credentials",
        "~/.aws/credentials",
        "~/.aws/config",
        "~/.docker/config.json",
        "~/.kube/config",
        "~/.config/gh/hosts.yml",
        "~/.config/fish/config.fish",
    ]
)
INDUS_SECRET_FILES = frozenset({"config.yaml", ".env", "auth.json", "secrets.json"})
SAFE_DEVICES = frozenset(
    {"/dev/null", "/dev/zero", "/dev/stdout", "/dev/stderr", "/dev/stdin", "/dev/tty", "/dev/full"}
)
_BLOCK_DEVICE = re.compile(
    r"^/dev/(?:sd[a-z]|hd[a-z]|vd[a-z]|xvd[a-z]|nvme\d|mmcblk\d|r?disk\d|md\d|dm-\d|loop\d|"
    r"mapper/|mtdblock\d|nbd\d|zd\d|xbd\d|sr\d)"
)
_ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\+?=(.*)$", re.DOTALL)
_SECRET_VAR = re.compile(
    r"\$\{?[A-Za-z0-9_]*(?:KEY|TOKEN|SECRET|PASSWD|PASSWORD|CREDENTIAL)[A-Za-z0-9_]*\}?", re.I
)
_HIDDEN = frozenset("​‌‎‏‪‫‬‭‮⁠⁡⁢⁣⁤⁦⁧⁨⁩﻿᠎")
_ZWJ = "‍"
_ANSI = re.compile(
    r"\x1b\[[0-9;?<>=]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b[@-Z\\-_]"
    r"|\x9b[0-9;?<>=]*[ -/]*[@-~]|\x9d[^\x07\x9c]*(?:\x07|\x9c)?"
)
_IFS = re.compile(r"\$\{IFS[^}]*\}|\$IFS(?![A-Za-z0-9_])")
_PARAM_DEFAULT = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*:?[-=+]([^{}$`'\"\s]*)\}")
_LITERAL_ECHO = re.compile(
    r"\$\(\s*(?:echo|printf\s+%s)\s+([A-Za-z0-9_./=:-]+)\s*\)|`\s*echo\s+([A-Za-z0-9_./=:-]+)\s*`"
)
_PARAM_REPLACE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*//?[^{}/]*/([^{}$`'\"\s]*)\}")
_FORK_FUNC = re.compile(
    r"(?:function\s+)?(?P<n>[A-Za-z_:.][\w:.]*)\s*(?:\(\s*\))?\s*\{\s*(?P=n)\s*\|\s*&?\s*(?P=n)"
    r"\s*&\s*;?\s*\}\s*;?\s*(?P=n)(?![\w:.])"
)
_EXEC_CALL = re.compile(
    r"(?:os\.system|os\.popen|os\.exec\w*|subprocess\.\w+|pty\.spawn|commands\.getoutput|"
    r"child_process|exec(?:Sync|File|FileSync)?|spawn(?:Sync)?|system|shell_exec|passthru|"
    r"proc_open|popen|IO\.popen|Open3\.\w+|Kernel\.system|Bun\.spawn(?:Sync)?|Deno\.Command|"
    r"Deno\.run)\s*\(",
)
_BACKTICK_FAMILIES = frozenset({"perl", "ruby", "php"})
_SHELL_QUOTE_CALL = re.compile(r"`|\bqx\s*[\(\{/]|%x[\(\{\[]")
_TREE_DELETE = re.compile(
    r"(?:shutil\.rmtree|rmtree|rm_rf|rm_r|remove_dir_all|rmSync|rmdirSync|FileUtils\.rm_rf|"
    r"FileUtils\.remove_dir|fs\.rm|Deno\.remove)\s*\(\s*(['\"])([^'\"]*)\1"
)
_STRING_LITERAL = re.compile(r"'((?:[^'\\]|\\.)*)'|\"((?:[^\"\\]|\\.)*)\"|`([^`]*)`")
_LIST_LITERAL = re.compile(r"\[([^\[\]]*)\]|\(([^()]*)\)")
_BARE_EXEC = re.compile(r"\b(?:system|exec)\s+[\"'\[]")


class _Stop(Exception):
    """Raised inside the analysis when a hardline finding makes the rest moot."""


@dataclass
class _Finding:
    key: str
    trace: tuple[str, ...]


@dataclass
class _Command:
    """A simple command after assignments and wrappers were taken off."""

    prog: str  # lower-case base name
    path: str  # the program word as written (homes folded)
    args: list[Word]
    raw: SimpleCommand
    assigns: dict[str, str] = field(default_factory=dict[str, str])
    wrappers: list[str] = field(default_factory=list[str])

    def texts(self) -> list[str]:
        return [w.text for w in self.args]


def _base(text: str) -> str:
    if not text:
        return ""
    return (PurePosixPath(text).name or text).lower()


def _is_python(prog: str) -> bool:
    return bool(_PYTHON.match(prog))


def _interpreter(prog: str) -> str | None:
    if _is_python(prog):
        return "python"
    for family, names in INTERPRETERS.items():
        if prog in names:
            return family
    return None


def _short_letters(arg: str) -> str:
    """Letters of a combined short-option word (``-rf`` → ``rf``); empty for anything else."""
    if arg.startswith("-") and not arg.startswith("--") and len(arg) > 1:
        return arg[1:]
    return ""


def _hidden_chars(text: str) -> bool:
    for index, ch in enumerate(text):
        if ch in _HIDDEN:
            return True
        if ch == _ZWJ:
            before = text[index - 1] if index else " "
            after = text[index + 1] if index + 1 < len(text) else " "
            if not (_is_symbol(before) and _is_symbol(after)):
                return True
    return False


def _is_symbol(ch: str) -> bool:
    return ord(ch) > 0x2000 and unicodedata.category(ch) in ("So", "Sk", "Mn", "Cf")


def normalise(text: str) -> str:
    """The text the lexer sees; see the module docstring, step 2."""
    text = _ANSI.sub("", text).replace("\x00", "")
    text = "".join(ch for ch in text if ch not in _HIDDEN and ch != _ZWJ)
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"[\x80-\x9f]", "", text)
    text = re.sub(r"\\\r?\n", "", text)
    text = _IFS.sub(" ", text)
    text = _PARAM_DEFAULT.sub(lambda m: m.group(1), text)
    text = _PARAM_REPLACE.sub(lambda m: m.group(1), text)
    return _LITERAL_ECHO.sub(lambda m: m.group(1) or m.group(2), text)


def _over_limits(text: str) -> bool:
    if len(text) > MAX_CHARS:
        return True
    separators = sum(text.count(ch) for ch in ";&|\n")
    if separators > MAX_SEPARATORS:
        return True
    return len(text) > MAX_UNBROKEN and separators == 0


class _Analyzer:
    def __init__(self, user_home: str | None, indus_home: str | None) -> None:
        self.user_home = _clean_home(user_home)
        self.indus_home = _clean_home(indus_home)
        self.findings: list[_Finding] = []

    # -- findings ------------------------------------------------------------------------------

    def flag(self, key: str, trace: Iterable[str], note: str = "") -> None:
        steps = (*trace, f"{key}: {note}" if note else key)
        self.findings.append(_Finding(key, steps))
        if key in HARDLINE:
            raise _Stop

    # -- paths ---------------------------------------------------------------------------------

    def fold(self, text: str) -> str:
        """Fold the literal Indus and user homes (and ``$HOME``) to ``$INDUS_HOME`` / ``~``."""
        for prefix in ("${INDUS_HOME}", "$INDUS_HOME"):
            if text == prefix or text.startswith(prefix + "/"):
                return "$INDUS_HOME" + text[len(prefix) :]
        if self.indus_home and (text == self.indus_home or text.startswith(self.indus_home + "/")):
            return "$INDUS_HOME" + text[len(self.indus_home) :]
        for prefix in ("${HOME}", "$HOME"):
            if text == prefix or text.startswith(prefix + "/"):
                return "~" + text[len(prefix) :]
        if self.user_home and (text == self.user_home or text.startswith(self.user_home + "/")):
            return "~" + text[len(self.user_home) :]
        return text

    def path(self, text: str) -> str:
        """Folded, lower-cased, with repeated slashes collapsed (case-insensitive file systems)."""
        folded = re.sub(r"/{2,}", "/", self.fold(text))
        return folded.lower()

    # -- scripts -------------------------------------------------------------------------------

    def script(self, text: str, depth: int, trace: tuple[str, ...]) -> None:
        if depth > MAX_DEPTH:
            self.flag("parser_limit", trace, f"payloads nested deeper than {MAX_DEPTH}")
        if _FORK_FUNC.search(text) or _FORK_FUNC.search(re.sub(r"\s+", "", text)):
            self.flag("fork_bomb", trace)
        try:
            commands = parse(text, max_words=MAX_WORDS)
        except ShellLimitError as exc:
            self.flag("parser_limit", trace, str(exc))
            return
        except ShellSyntaxError as exc:
            self.flag("malformed", trace, str(exc))
            return
        marked: set[str] = set()
        for pipe in pipelines(commands):
            stages: list[_Command | None] = []
            for raw in pipe:
                step = (*trace, "command: " + _show(raw))
                stages.append(self.resolve(raw, depth, step))
            for index, stage in enumerate(stages):
                raw = pipe[index]
                unwrapped = stage.wrappers if stage is not None else []
                step = (*trace, "command: " + _show(raw), *(f"unwrapped {w}" for w in unwrapped))
                self.redirects(raw, stage, depth, step)
                if stage is not None:
                    self.command(stage, depth, step, stages[:index], marked)
        for raw in commands:
            words = [*raw.words, *(r.target for r in raw.redirects)]
            for word in words:
                for sub in word.subs:
                    self.script(sub.text, depth + 1, (*trace, f"inside {sub.kind}…)"))

    # -- unwrapping ----------------------------------------------------------------------------

    def resolve(self, raw: SimpleCommand, depth: int, trace: tuple[str, ...]) -> _Command | None:
        words = list(raw.words)
        assigns: dict[str, str] = {}
        wrappers: list[str] = []
        for _ in range(64):
            while words and not words[0].quoted and words[0].text in KEYWORDS:
                words = words[1:]
            if words and not words[0].quoted and words[0].text == "function":
                words = words[2:]
                continue
            while words and (match := _ASSIGNMENT.match(words[0].text)):
                assigns[match.group(1)] = match.group(2)
                words = words[1:]
            if not words:
                return _Command("", "", [], raw, assigns, wrappers)
            if not words[0].quoted and words[0].text in CLOSERS:
                return None
            prog = _base(words[0].text)
            unwrap = _WRAPPERS.get(prog)
            if unwrap is None:
                break
            step = (*trace, f"unwrapped {prog}")
            rest = unwrap(self, words[1:], depth, step)
            if rest is None:
                return None
            wrappers.append(prog)
            words = rest
        else:
            self.flag("parser_limit", trace, "too many wrappers")
        prog = _base(words[0].text)
        return _Command(prog, self.fold(words[0].text), words[1:], raw, assigns, wrappers)

    # -- the rules -----------------------------------------------------------------------------

    def command(
        self,
        cmd: _Command,
        depth: int,
        trace: tuple[str, ...],
        before: Sequence[_Command | None],
        marked: set[str],
    ) -> None:
        for name in cmd.assigns:
            if name in ("DOCKER_HOST", "CONTAINER_HOST", "DOCKER_CONTEXT"):
                self.flag("docker_remote", trace, f"{name}= on the command")
        if not cmd.prog:
            return
        program_word = cmd.raw.words[len(cmd.raw.words) - len(cmd.args) - 1]
        if any(self.fetches(sub.text) for sub in program_word.subs):
            self.flag("remote_exec", trace, "program name comes from a download")
        rule = _RULES.get(cmd.prog)
        if rule is not None:
            rule(self, cmd, depth, trace)
        if cmd.prog.startswith(("mkfs", "mke2fs", "mkswap", "newfs", "mkdosfs", "mkntfs")):
            self.flag("mkfs", trace, cmd.prog)
        if cmd.prog in SHELLS:
            self.shell(cmd, depth, trace, before)
        elif (family := _interpreter(cmd.prog)) is not None:
            self.interp(family, cmd, depth, trace, before)
        if cmd.prog in SQL_VERBS:
            self.sql(" ".join([cmd.path, *cmd.texts()]), trace)
        if cmd.prog in SQL_CLIENTS:
            self.sql(" ; ".join(_fed_text(before)), trace)
        if cmd.prog in FETCHERS and _uploads_stdin(cmd):
            fed = [e for e in before if e is not None]
            if any(self.reads_credential(e) for e in fed) or self.reads_credential(cmd):
                self.flag("secret_upload", trace, f"credential file piped into {cmd.prog}")
        self.exec_marked(cmd, trace, marked)

    # redirections ---------------------------------------------------------------------------

    def redirects(
        self, raw: SimpleCommand, cmd: _Command | None, depth: int, trace: tuple[str, ...]
    ) -> None:
        for redirect in raw.redirects:
            writes = redirect.op in (">", ">>", ">|", "&>", "&>>", "<>") or (
                redirect.op == ">&" and not redirect.target.text.isdigit()
            )
            if writes and redirect.target.text != "-":
                self.write_target(redirect.target.text, trace, project=True, how="redirect")

    def write_target(self, text: str, trace: tuple[str, ...], *, project: bool, how: str) -> None:
        p = self.path(text)
        if p.startswith("/dev/"):
            if _BLOCK_DEVICE.match(p):
                self.flag("block_device_write", trace, f"{how} to {text}")
            elif p not in SAFE_DEVICES and not p.startswith(("/dev/fd/", "/dev/tcp/", "/dev/udp/")):
                self.flag("disk_tool", trace, f"{how} to {text}")
            return
        key = self.classify_write(p, project=project)
        if key is not None:
            self.flag(key, trace, f"{how} to {text}")

    def classify_write(self, p: str, *, project: bool) -> str | None:
        name = PurePosixPath(p).name
        in_home = p.startswith(("$indus_home/", "~/.indus/"))
        if in_home and (name in INDUS_SECRET_FILES or name == "secrets.index.json"):
            return "indus_config_write"
        if self.is_user_secret(p):
            return "user_secret_write"
        if p.startswith(SYSTEM_CONFIG_PREFIXES):
            return "system_config_write"
        relative = not p.startswith(("/", "~", "$"))
        env_like = name == ".env" or name.startswith(".env.") or name == "config.yaml"
        if project and relative and env_like:
            return "project_env_write"
        return None

    def is_user_secret(self, p: str) -> bool:
        if p in ("~/.ssh", "~/.gnupg") or p.startswith(("~/.ssh/", "~/.gnupg/")):
            return True
        if p.startswith("~/") and p[2:] in SHELL_RC:
            return True
        return p in HOME_CREDENTIALS

    def is_credential_file(self, text: str) -> bool:
        p = self.path(text.lstrip("@<"))
        name = PurePosixPath(p).name
        if name == ".env" or name.startswith(".env."):
            return True
        if self.is_user_secret(p) or p in ("/etc/passwd", "/etc/shadow", "/etc/sudoers"):
            return True
        if (p.startswith(("$indus_home/", "~/.indus/"))) and name in INDUS_SECRET_FILES:
            return True
        return name.endswith((".pem", ".key")) or name.startswith(
            ("id_rsa", "id_ed25519", "id_ecdsa")
        )

    def reads_credential(self, cmd: _Command) -> bool:
        """``cmd`` prints a credential file (as an operand or through ``<``)."""
        for redirect in cmd.raw.redirects:
            if redirect.op == "<" and self.is_credential_file(redirect.target.text):
                return True
        if cmd.prog in FETCHERS:
            return False
        return any(self.is_credential_file(t) for t in _operands(cmd.texts()))

    # helpers for payloads -------------------------------------------------------------------

    def fetches(self, text: str) -> bool:
        """True when ``text`` (a substitution) runs a download tool."""
        try:
            commands = parse(text, max_words=MAX_WORDS)
        except (ShellSyntaxError, ShellLimitError):
            return False
        for raw in commands:
            words = [w for w in raw.words if not _ASSIGNMENT.match(w.text)]
            while words and _base(words[0].text) in _WRAPPERS:
                words = words[1:]
            if words and _base(words[0].text) in FETCHERS:
                return True
        return False

    def reads_stdin_from_download(self, cmd: _Command) -> bool:
        for redirect in cmd.raw.redirects:
            if redirect.op == "<" and any(self.fetches(s.text) for s in redirect.target.subs):
                return True
        return False

    def runner(
        self,
        cmd: _Command,
        trace: tuple[str, ...],
        before: Sequence[_Command | None],
        depth: int,
    ) -> None:
        """``cmd`` executes whatever arrives on stdin: check what feeds it."""
        if self.reads_stdin_from_download(cmd):
            self.flag("remote_exec", trace, f"{cmd.prog} reads a download")
        for redirect in cmd.raw.redirects:
            if redirect.op in ("<<", "<<-", "<<<"):
                self.flag("heredoc_exec", trace, f"{redirect.op} into {cmd.prog}")
        for earlier in before:
            if earlier is None:
                continue
            if earlier.prog in FETCHERS:
                self.flag("remote_exec", trace, f"{earlier.prog} piped into {cmd.prog}")
            if _is_decoder(earlier):
                self.flag("decode_exec", trace, f"{earlier.prog} piped into {cmd.prog}")
        fed_heredoc = any(
            r.op in ("<<", "<<-", "<<<") for e in before if e is not None for r in e.raw.redirects
        )
        if fed_heredoc:
            self.flag("heredoc_exec", trace, f"here-document piped into {cmd.prog}")
        if cmd.prog in SHELLS:
            for redirect in cmd.raw.redirects:
                if redirect.op in ("<<", "<<-") and redirect.body:
                    self.script(redirect.body, depth + 1, (*trace, "here-document body"))
                elif redirect.op == "<<<":
                    self.script(redirect.target.text, depth + 1, (*trace, "here-string"))
            for text in _fed_text(before):
                self.script(text, depth + 1, (*trace, "text piped into a shell"))

    # shells and interpreters ----------------------------------------------------------------

    def shell(
        self,
        cmd: _Command,
        depth: int,
        trace: tuple[str, ...],
        before: Sequence[_Command | None],
    ) -> None:
        args = cmd.args
        command_mode = False
        stdin_mode = False
        operand: Word | None = None
        index = 0
        while index < len(args):
            text = args[index].text
            if text == "--":
                operand = args[index + 1] if index + 1 < len(args) else None
                break
            if text in ("-o", "+o", "-O", "+O", "--rcfile", "--init-file"):
                index += 2
                continue
            if text.startswith("--"):
                index += 1
                continue
            if len(text) > 1 and text[0] in "-+":
                command_mode = command_mode or "c" in text[1:]
                stdin_mode = stdin_mode or "s" in text[1:]
                index += 1
                continue
            operand = args[index]
            break
        if command_mode:
            if operand is not None:
                self.script(operand.text, depth + 1, (*trace, f"payload of {cmd.prog} -c"))
            return
        if operand is None or stdin_mode or operand.text == "-":
            self.runner(cmd, trace, before, depth)
            return
        if any(self.fetches(s.text) for s in operand.subs):
            self.flag("remote_exec", trace, f"{cmd.prog} runs a downloaded script")

    def interp(
        self,
        family: str,
        cmd: _Command,
        depth: int,
        trace: tuple[str, ...],
        before: Sequence[_Command | None],
    ) -> None:
        args = cmd.args
        texts = cmd.texts()
        if family == "python" and "-m" in texts:
            module = texts[texts.index("-m") + 1 :]
            if module[:1] and module[0] in ("pip", "pip3", "pipx") and "uninstall" in module[1:]:
                self.flag("package_remove", trace, f"python -m {module[0]} uninstall")
            if not any(t.startswith("-c") for t in texts[: texts.index("-m")]):
                return
        exec_flags = _EXEC_FLAGS[family]
        letter = _EXEC_LETTER.get(family)
        arg_opts = _INTERP_ARG_OPTS.get(family, frozenset[str]())
        operand: Word | None = None
        index = 0
        while index < len(args):
            text = args[index].text
            nxt = args[index + 1].text if index + 1 < len(args) else None
            if text == "--":
                operand = args[index + 1] if index + 1 < len(args) else None
                break
            payload: str | None = None
            if text in exec_flags:
                payload = nxt
            elif "=" in text and text.split("=", 1)[0] in exec_flags and text.startswith("--"):
                payload = text.split("=", 1)[1]
            elif family == "python" and text.startswith("-c") and len(text) > 2:
                payload = text[2:]
            elif letter and re.fullmatch(r"-[A-Za-z]+", text) and letter in text[1:]:
                payload = nxt
            if payload is not None:
                self.payload(family, payload, depth + 1, (*trace, f"payload of {cmd.prog}"))
                return
            if text in arg_opts:
                index += 2
                continue
            if text.startswith("-"):
                index += 1
                continue
            operand = args[index]
            break
        if family == "deno" and operand is not None and operand.text == "eval":
            later = args[index + 1 :]
            if later:
                self.payload(family, later[0].text, depth + 1, (*trace, "payload of deno eval"))
            return
        if operand is None or operand.text == "-":
            self.runner(cmd, trace, before, depth)
            return
        if any(self.fetches(s.text) for s in operand.subs):
            self.flag("remote_exec", trace, f"{cmd.prog} runs a downloaded script")

    def payload(self, family: str, code: str, depth: int, trace: tuple[str, ...]) -> None:
        """Interpreter source: look for shell commands it runs and trees it deletes."""
        if depth > MAX_DEPTH:
            self.flag("parser_limit", trace, f"payloads nested deeper than {MAX_DEPTH}")
        lowered = code.lower()
        if "fork" in lowered and re.search(r"\bwhile\b|\bfor\s*\(\s*;|\bloop\b|\buntil\b", lowered):
            self.flag("fork_bomb", trace, f"{family} fork loop")
        for match in _TREE_DELETE.finditer(code):
            target = match.group(2)
            self.script("rm -rf " + shlex.quote(target), depth + 1, (*trace, "tree delete call"))
        runs_shell = bool(_EXEC_CALL.search(code))
        if family in _BACKTICK_FAMILIES and _SHELL_QUOTE_CALL.search(code):
            runs_shell = True
        if family in ("perl", "ruby") and _BARE_EXEC.search(code):
            runs_shell = True
        if family == "bun" and "$`" in code:
            runs_shell = True
        if not runs_shell:
            return
        covered: list[tuple[int, int]] = []
        for match in _LIST_LITERAL.finditer(code):
            inside = match.group(1) or match.group(2) or ""
            items = [a or b or "" for a, b, _ in _STRING_LITERAL.findall(inside)]
            if len(items) > 1:
                covered.append(match.span())
                joined = " ".join(shlex.quote(item) for item in items)
                self.script(joined, depth + 1, (*trace, "argument list run by the payload"))
        for match in _STRING_LITERAL.finditer(code):
            if any(start <= match.start() < end for start, end in covered):
                continue
            literal = match.group(1) or match.group(2) or match.group(3) or ""
            if literal.strip():
                self.script(literal, depth + 1, (*trace, "string run by the payload"))

    # SQL ------------------------------------------------------------------------------------

    def sql(self, text: str, trace: tuple[str, ...]) -> None:
        for statement in text.split(";"):
            s = " ".join(statement.lower().replace("(", " ").split())
            drops = re.match(r"drop\s+(?:table|database|schema|view|user|role|index)\b", s)
            empties = re.match(r"truncate\b", s)
            unbounded = re.match(r"delete\s+from\s+\S+", s) and not re.search(r"\bwhere\b", s)
            if drops or empties or unbounded:
                self.flag("sql_destructive", trace, s[:60])

    # chmod +x then run ------------------------------------------------------------------------

    def exec_marked(self, cmd: _Command, trace: tuple[str, ...], marked: set[str]) -> None:
        if cmd.prog == "chmod":
            texts = cmd.texts()
            operands = [t for t in texts if not t.startswith("-")]
            if len(operands) >= 2 and _adds_exec(operands[0]):
                marked.update(_plain_path(t) for t in operands[1:])
            return
        candidates = [cmd.path]
        if cmd.prog in SHELLS or _interpreter(cmd.prog) is not None:
            candidates += [t for t in cmd.texts() if not t.startswith("-")][:1]
        if marked and any(_plain_path(c) in marked for c in candidates):
            self.flag("chmod_exec_run", trace, cmd.path)


# -- per-program rules ---------------------------------------------------------------------------

Rule = Callable[[_Analyzer, _Command, int, tuple[str, ...]], None]


def _operands(texts: Sequence[str]) -> list[str]:
    out: list[str] = []
    after_dashes = False
    for text in texts:
        if after_dashes or not text.startswith("-") or text == "-":
            out.append(text)
        elif text == "--":
            after_dashes = True
    return out


def _flags(texts: Sequence[str]) -> tuple[set[str], set[str]]:
    letters: set[str] = set()
    longs: set[str] = set()
    for text in texts:
        if text == "--":
            break
        if text.startswith("--"):
            longs.add(text.split("=", 1)[0])
        elif text.startswith("-") and len(text) > 1:
            letters.update(text[1:])
    return letters, longs


def _rule_rm(a: _Analyzer, cmd: _Command, depth: int, trace: tuple[str, ...]) -> None:
    texts = cmd.texts()
    letters, longs = _flags(texts)
    recursive = bool({"r", "R"} & letters) or "--recursive" in longs
    for target in _operands(texts):
        p = a.path(target)
        bare = p.rstrip("/") or "/"
        top = re.sub(r"/(?:\*|\.\*?)$", "", bare) or "/"
        note = f"rm {'-r ' if recursive else ''}{target}"
        if recursive and (top == "/" or bare in ("/*", "/.")):
            a.flag("rm_root", trace, note)
        if recursive and top in ("~", "~/."):
            a.flag("rm_home", trace, note)
        if recursive and top in SYSTEM_DIRS:
            a.flag("rm_system", trace, note)
        if not recursive:
            protected = top in SYSTEM_DIRS or p.startswith(SYSTEM_CONFIG_PREFIXES)
            protected = protected or a.is_user_secret(p)
            if protected or a.classify_write(p, project=False) == "indus_config_write":
                a.flag("rm_protected", trace, note)
            continue
        if _risky_target(p):
            a.flag("rm_recursive", trace, note)


def _risky_target(p: str) -> bool:
    if p.startswith(("/", "~", "$", "`")) or "$" in p or "`" in p:
        return True
    if any(part == ".." for part in p.split("/")):
        return True
    return p in (".", "./", "*", "./*", ".*", "./.*") or p.startswith(("*", ".*"))


def _rule_shred(a: _Analyzer, cmd: _Command, depth: int, trace: tuple[str, ...]) -> None:
    for target in _operands(cmd.texts()):
        p = a.path(target)
        if p.startswith("/dev/") and p not in SAFE_DEVICES:
            a.flag("disk_tool", trace, f"{cmd.prog} {target}")
        elif p.startswith(SYSTEM_CONFIG_PREFIXES) or a.is_user_secret(p):
            a.flag("rm_protected", trace, f"{cmd.prog} {target}")


def _rule_find(a: _Analyzer, cmd: _Command, depth: int, trace: tuple[str, ...]) -> None:
    args = cmd.args
    index = 0
    while index < len(args):
        word = args[index]
        text = word.text
        if text == "-delete":
            a.flag("find_delete", trace, "find -delete")
        if not word.quoted and word.dynamic and text.startswith("-"):
            a.flag("dynamic_word", trace, f"find option {text}")
        if text in ("-exec", "-execdir", "-ok", "-okdir"):
            end = index + 1
            while end < len(args) and args[end].text not in (";", "+"):
                end += 1
            inner = args[index + 1 : end]
            if inner and _base(inner[0].text) in DELETERS:
                a.flag("find_delete", trace, f"find {text} {inner[0].text}")
            elif inner:
                line = " ".join(shlex.quote(w.text.replace("{}", "FILE")) for w in inner)
                a.script(line, depth + 1, (*trace, f"command of find {text}"))
            index = end
        index += 1


_XARGS_ARG_OPTS = frozenset({"-I", "-n", "-P", "-L", "-l", "-d", "-E", "-e", "-s", "-a"})


def _rule_xargs(a: _Analyzer, cmd: _Command, depth: int, trace: tuple[str, ...]) -> None:
    args = cmd.texts()
    index = 0
    while index < len(args) and args[index].startswith("-"):
        if args[index] == "--":
            index += 1
            break
        index += 2 if args[index] in _XARGS_ARG_OPTS else 1
    inner = args[index:]
    if not inner:
        return
    if _base(inner[0]) in DELETERS:
        a.flag("xargs_delete", trace, f"xargs {inner[0]}")
        return
    line = " ".join(shlex.quote(t) for t in inner)
    a.script(line, depth + 1, (*trace, "command of xargs"))


def _world_writable(mode: str) -> bool:
    if re.fullmatch(r"[0-7]{3,4}", mode):
        return mode[-1] in "2367"
    for clause in mode.split(","):
        match = re.fullmatch(r"([ugoa]*)([+=])([rwxXst]*)", clause)
        if match and "w" in match.group(3) and ("o" in match.group(1) or "a" in match.group(1)):
            return True
    return False


def _adds_exec(mode: str) -> bool:
    if re.fullmatch(r"[0-7]{3,4}", mode):
        return int(mode[-3]) % 2 == 1
    return bool(re.fullmatch(r"[ugoa]*\+[rwxXst]*x[rwxXst]*", mode))


def _plain_path(text: str) -> str:
    return text[2:] if text.startswith("./") else text


def _rule_chmod(a: _Analyzer, cmd: _Command, depth: int, trace: tuple[str, ...]) -> None:
    operands = [t for t in cmd.texts() if not t.startswith("-") or re.fullmatch(r"-[rwx]+", t)]
    if operands and _world_writable(operands[0]):
        a.flag("chmod_world_writable", trace, f"chmod {operands[0]}")


def _rule_chown(a: _Analyzer, cmd: _Command, depth: int, trace: tuple[str, ...]) -> None:
    texts = cmd.texts()
    letters, longs = _flags(texts)
    operands = _operands(texts)
    if ("R" in letters or "--recursive" in longs) and operands:
        owner = operands[0].split(":", 1)[0].split(".", 1)[0]
        if owner in ("root", "0"):
            a.flag("chown_root_recursive", trace, f"chown -R {operands[0]}")


_RANDOM_DEVICES = frozenset({"/dev/urandom", "/dev/random"})


def _rule_dd(a: _Analyzer, cmd: _Command, depth: int, trace: tuple[str, ...]) -> None:
    for text in cmd.texts():
        key, _, value = text.partition("=")
        p = a.path(value)
        if key == "of":
            a.write_target(value, trace, project=True, how="dd")
        elif key == "if" and p.startswith("/dev/") and p not in SAFE_DEVICES | _RANDOM_DEVICES:
            a.flag("disk_tool", trace, f"dd reads {value}")


def _rule_disk(a: _Analyzer, cmd: _Command, depth: int, trace: tuple[str, ...]) -> None:
    texts = cmd.texts()
    listing = any(t in ("-l", "--list", "print", "-p", "--print") for t in texts) or not texts
    if cmd.prog in ("fdisk", "sfdisk", "gdisk", "sgdisk", "parted") and listing:
        return
    if cmd.prog == "diskutil" and not any(
        t.lower().startswith(("erase", "partition", "zero", "random", "secureerase", "reformat"))
        for t in texts
    ):
        return
    if cmd.prog == "cryptsetup" and not any(t in ("luksFormat", "erase", "format") for t in texts):
        return
    a.flag("disk_tool", trace, cmd.prog)


def _rule_redirecting_writer(
    a: _Analyzer, cmd: _Command, depth: int, trace: tuple[str, ...]
) -> None:
    """tee and the copy family: the target path decides."""
    texts = cmd.texts()
    if cmd.prog == "tee":
        for target in _operands(texts):
            a.write_target(target, trace, project=True, how="tee")
        return
    targets: list[str] = []
    for index, text in enumerate(texts):
        if text in ("-t", "--target-directory") and index + 1 < len(texts):
            targets.append(texts[index + 1])
        elif text.startswith("--target-directory="):
            targets.append(text.split("=", 1)[1])
    operands = _operands(texts)
    if not targets and len(operands) >= 2:
        targets.append(operands[-1])
    for target in targets:
        a.write_target(target, trace, project=True, how=cmd.prog)


def _rule_inplace(a: _Analyzer, cmd: _Command, depth: int, trace: tuple[str, ...]) -> None:
    texts = cmd.texts()
    letters, longs = _flags(texts)
    if cmd.prog == "sed":
        in_place = "i" in letters or "--in-place" in longs
    else:  # perl / ruby with -i[ext]
        in_place = any(re.fullmatch(r"-[a-zA-Z]*i\S*", t) for t in texts)
    if not in_place:
        return
    for target in _operands(texts):
        key = a.classify_write(a.path(target), project=False)
        if key is not None:
            a.flag(key, trace, f"{cmd.prog} in-place edit of {target}")


def _rule_systemctl(a: _Analyzer, cmd: _Command, depth: int, trace: tuple[str, ...]) -> None:
    texts = cmd.texts()
    index = 0
    while index < len(texts) and texts[index].startswith("-"):
        index += 2 if texts[index] in ("-H", "--host", "-M", "--machine", "-t", "--type") else 1
    verb = texts[index].lower() if index < len(texts) else ""
    if verb in ("poweroff", "reboot", "halt", "kexec", "soft-reboot", "rescue", "emergency"):
        a.flag("power", trace, f"systemctl {verb}")
    if verb in ("stop", "restart", "disable", "mask", "kill", "isolate", "try-restart"):
        a.flag("service_change", trace, f"systemctl {verb}")
    if verb == "reload-or-restart":
        a.flag("service_change", trace, f"systemctl {verb}")


def _rule_service(a: _Analyzer, cmd: _Command, depth: int, trace: tuple[str, ...]) -> None:
    texts = [t.lower() for t in cmd.texts()]
    verbs = {"stop", "restart", "disable", "down", "kill", "unload", "bootout", "remove"}
    if cmd.prog == "launchctl":
        if (
            texts
            and texts[0] in verbs | {"kickstart"}
            and (texts[0] != "kickstart" or "-k" in texts)
        ):
            a.flag("service_change", trace, f"launchctl {texts[0]}")
        return
    if any(t in verbs for t in texts):
        a.flag("service_change", trace, f"{cmd.prog} {' '.join(texts)}")


def _rule_power(a: _Analyzer, cmd: _Command, depth: int, trace: tuple[str, ...]) -> None:
    texts = cmd.texts()
    if cmd.prog == "shutdown" and any(t in ("-c", "--help") for t in texts):
        return
    if cmd.prog in ("init", "telinit"):
        if texts and texts[0] in ("0", "6"):
            a.flag("power", trace, f"{cmd.prog} {texts[0]}")
        return
    if any(t in ("--help", "--version") for t in texts):
        return
    a.flag("power", trace, cmd.prog)


_KILL_SIGNALS = frozenset({"9", "KILL", "SIGKILL"})


def _rule_kill(a: _Analyzer, cmd: _Command, depth: int, trace: tuple[str, ...]) -> None:
    args = cmd.args
    signal = ""
    index = 0
    if args and args[0].text in ("-s", "-n", "--signal") and len(args) > 1:
        signal, index = args[1].text, 2
    elif args and args[0].text.startswith("-") and args[0].text != "--":
        if len(args) == 1:
            return
        signal, index = args[0].text[1:], 1
    if index < len(args) and args[index].text == "--":
        index += 1
    targets = args[index:]
    if any(t.text == "-1" for t in targets):
        a.flag("kill_all", trace, "kill -1")
    if any(_names_indus(s.text) for t in targets for s in t.subs):
        a.flag("kill_self", trace, "kill of an Indus process id")
    if signal.upper() in _KILL_SIGNALS and targets:
        a.flag("kill_force", trace, f"kill -{signal}")


def _names_indus(text: str) -> bool:
    lowered = text.lower()
    return bool(re.search(r"\b(?:pgrep|pidof)\b", lowered)) and "indus" in lowered


def _rule_pkill(a: _Analyzer, cmd: _Command, depth: int, trace: tuple[str, ...]) -> None:
    texts = cmd.texts()
    if cmd.prog == "killall5":
        a.flag("kill_all", trace, "killall5")
    if cmd.prog == "killall" and any(
        t in ("-l", "--list", "-V", "--version", "--help") for t in texts
    ):
        return
    if any("indus" in t.lower() for t in _operands(texts)):
        a.flag("kill_self", trace, f"{cmd.prog} {' '.join(texts)}")
    force = False
    for index, text in enumerate(texts):
        if text.lstrip("-").upper() in _KILL_SIGNALS and text.startswith("-"):
            force = True
        if text in ("-s", "--signal") and index + 1 < len(texts):
            force = force or texts[index + 1].upper() in _KILL_SIGNALS
        if text.startswith("--signal=") and text.split("=", 1)[1].upper() in _KILL_SIGNALS:
            force = True
        if cmd.prog == "killall" and text in ("-r", "--regexp"):
            force = True
    if force:
        a.flag("kill_force", trace, f"{cmd.prog} {' '.join(texts)}")


def _rule_fetcher(a: _Analyzer, cmd: _Command, depth: int, trace: tuple[str, ...]) -> None:
    texts = cmd.texts()
    for text in texts:
        if any(host in text.lower() for host in METADATA_HOSTS):
            a.flag("metadata_endpoint", trace, text)
    data_opts = tuple(
        [
            "-d",
            "--data",
            "--data-raw",
            "--data-binary",
            "--data-urlencode",
            "--data-ascii",
            "--json",
            "-F",
            "--form",
            "--form-string",
            "-T",
            "--upload-file",
            "--post-data",
            "--post-file",
            "--body-data",
            "--body-file",
        ]
    )
    for index, text in enumerate(texts):
        value: str | None = None
        if text in data_opts and index + 1 < len(texts):
            value = texts[index + 1]
        elif "=" in text and text.split("=", 1)[0] in data_opts:
            value = text.split("=", 1)[1]
        elif re.fullmatch(r"-[dFT].+", text):
            value = text[2:]
        if value is None:
            continue
        file_part = (
            value.split("=", 1)[1] if "=" in value and text.startswith(("-F", "--form")) else value
        )
        file_part = file_part.split(";", 1)[0]
        is_file = text.startswith(
            ("-T", "--upload-file", "--post-file", "--body-file")
        ) or file_part.startswith(("@", "<"))
        if _SECRET_VAR.search(value):
            a.flag("secret_upload", trace, f"{cmd.prog} sends {value}")
        elif is_file and file_part.lstrip("@<") != "-" and a.is_credential_file(file_part):
            a.flag("secret_upload", trace, f"{cmd.prog} uploads {file_part}")


def _rule_git(a: _Analyzer, cmd: _Command, depth: int, trace: tuple[str, ...]) -> None:
    texts = cmd.texts()
    index = 0
    with_value = ("-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env")
    while index < len(texts) and texts[index].startswith("-"):
        index += 2 if texts[index] in with_value else 1
    if index >= len(texts):
        return
    verb = texts[index].lower()
    rest = texts[index + 1 :]
    letters, longs = _flags(rest)
    if verb == "reset" and "--hard" in longs:
        a.flag("git_destructive", trace, "git reset --hard")
    elif verb == "push" and (
        "f" in letters
        or longs & {"--force", "--force-with-lease", "--mirror"}
        or any(t.startswith("+") for t in _operands(rest))
    ):
        a.flag("git_destructive", trace, "git push --force")
    elif verb == "clean" and ("f" in letters or "--force" in longs):
        if not ("n" in letters or "--dry-run" in longs):
            a.flag("git_destructive", trace, "git clean -f")
    elif verb == "branch" and (
        "D" in letters
        or (("d" in letters or "--delete" in longs) and ("f" in letters or "--force" in longs))
    ):
        a.flag("git_destructive", trace, "git branch force delete")


_PACKAGE_REMOVE: dict[str, frozenset[str]] = {
    "apt": frozenset({"remove", "purge", "autoremove"}),
    "apt-get": frozenset({"remove", "purge", "autoremove"}),
    "aptitude": frozenset({"remove", "purge"}),
    "yum": frozenset({"remove", "erase", "autoremove"}),
    "dnf": frozenset({"remove", "erase", "autoremove"}),
    "microdnf": frozenset({"remove"}),
    "zypper": frozenset({"remove", "rm"}),
    "apk": frozenset({"del"}),
    "brew": frozenset({"uninstall", "remove", "rm"}),
    "port": frozenset({"uninstall"}),
    "snap": frozenset({"remove"}),
    "flatpak": frozenset({"uninstall", "remove"}),
    "pip": frozenset({"uninstall"}),
    "pip3": frozenset({"uninstall"}),
    "pipx": frozenset({"uninstall", "uninstall-all"}),
    "gem": frozenset({"uninstall"}),
    "cargo": frozenset({"uninstall"}),
}


def _rule_packages(a: _Analyzer, cmd: _Command, depth: int, trace: tuple[str, ...]) -> None:
    texts = cmd.texts()
    words = [t.lower() for t in texts if not t.startswith("-")]
    letters, longs = _flags(texts)
    hit = False
    if cmd.prog in _PACKAGE_REMOVE:
        hit = bool(words) and words[0] in _PACKAGE_REMOVE[cmd.prog]
    elif cmd.prog == "pacman":
        hit = any(re.fullmatch(r"-R\w*", t) for t in texts) or "--remove" in longs
    elif cmd.prog in ("dpkg", "rpm"):
        hit = bool({"r", "P", "e"} & letters) or bool({"--remove", "--purge", "--erase"} & longs)
    elif cmd.prog in ("npm", "pnpm"):
        remove = {"uninstall", "remove", "rm", "un", "r", "unlink"}
        hit = bool(words) and words[0] in remove and ("g" in letters or "--global" in longs)
    elif cmd.prog == "yarn":
        hit = words[:2] == ["global", "remove"]
    elif cmd.prog == "uv":
        hit = words[:2] in (["pip", "uninstall"], ["tool", "uninstall"])
    if hit:
        a.flag("package_remove", trace, f"{cmd.prog} {' '.join(words[:2])}")


_DOCKER_VALUE_OPTS = frozenset(
    [
        "--config",
        "-l",
        "--log-level",
        "--tlscacert",
        "--tlscert",
        "--tlskey",
        "--root",
        "--runroot",
        "--storage-driver",
        "--cgroup-manager",
        "--tmpdir",
        "--events-backend",
    ]
)
_DOCKER_REMOTE_OPTS = frozenset(
    {"-H", "--host", "--context", "--url", "--connection", "--identity", "-r", "--remote"}
)
_COMPOSE_VALUE_OPTS = frozenset(
    {"-f", "--file", "-p", "--project-name", "--project-directory", "--env-file", "--profile"}
)


def _rule_docker(a: _Analyzer, cmd: _Command, depth: int, trace: tuple[str, ...]) -> None:
    texts = cmd.texts()
    index = 0
    if cmd.prog not in ("docker-compose", "podman-compose"):
        while index < len(texts) and texts[index].startswith("-"):
            text = texts[index]
            name = text.split("=", 1)[0]
            context_flag = text == "-c" and cmd.prog in ("docker", "podman")
            if name in _DOCKER_REMOTE_OPTS or context_flag or re.fullmatch(r"-H\S+", text):
                a.flag("docker_remote", trace, f"{cmd.prog} {text}")
            takes_value = name in _DOCKER_VALUE_OPTS or name in _DOCKER_REMOTE_OPTS or context_flag
            if name in ("-r", "--remote"):
                takes_value = False
            index += 2 if takes_value and "=" not in text and len(text) <= len(name) else 1
    rest = [t.lower() for t in texts[index:]]
    if not rest and cmd.prog not in ("docker-compose", "podman-compose"):
        return
    verbs = {"stop", "kill", "restart"}
    compose = cmd.prog in ("docker-compose", "podman-compose") or (rest[:1] == ["compose"])
    if compose:
        tail = rest[1:] if rest[:1] == ["compose"] else rest
        position = 0
        while position < len(tail) and tail[position].startswith("-"):
            position += 2 if tail[position] in _COMPOSE_VALUE_OPTS else 1
        if position < len(tail) and tail[position] in verbs | {"down"}:
            a.flag("docker_lifecycle", trace, f"compose {tail[position]}")
        return
    if rest[:2] == ["context", "use"]:
        a.flag("docker_remote", trace, f"{cmd.prog} context use")
    elif rest[:1] and rest[0] in verbs:
        a.flag("docker_lifecycle", trace, f"{cmd.prog} {rest[0]}")
    elif rest[:1] == ["container"] and len(rest) > 1 and rest[1] in verbs:
        a.flag("docker_lifecycle", trace, f"{cmd.prog} container {rest[1]}")


def _rule_export(a: _Analyzer, cmd: _Command, depth: int, trace: tuple[str, ...]) -> None:
    for text in cmd.texts():
        name = text.split("=", 1)[0]
        if "=" in text and name in ("DOCKER_HOST", "CONTAINER_HOST", "DOCKER_CONTEXT"):
            a.flag("docker_remote", trace, f"export {name}")


def _rule_eval(a: _Analyzer, cmd: _Command, depth: int, trace: tuple[str, ...]) -> None:
    joined = " ".join(w.text for w in cmd.args)
    if joined.strip():
        a.script(joined, depth + 1, (*trace, "payload of eval"))


def _rule_source(a: _Analyzer, cmd: _Command, depth: int, trace: tuple[str, ...]) -> None:
    for word in cmd.args[:1]:
        if any(a.fetches(s.text) for s in word.subs):
            a.flag("remote_exec", trace, f"{cmd.prog} of a download")
    if a.reads_stdin_from_download(cmd):
        a.flag("remote_exec", trace, f"{cmd.prog} of a download")


def _rule_carrier(a: _Analyzer, cmd: _Command, depth: int, trace: tuple[str, ...]) -> None:
    """``su -c``, ``runuser -c``, ``script -c``, ``watch``: their argument is a shell command."""
    texts = cmd.texts()
    if cmd.prog == "watch":
        index = 0
        while index < len(texts) and texts[index].startswith("-"):
            index += 2 if texts[index] in ("-n", "--interval", "-d") else 1
        payload = " ".join(texts[index:])
        if payload:
            a.script(payload, depth + 1, (*trace, "command of watch"))
        return
    for index, text in enumerate(texts):
        if text in ("-c", "--command") and index + 1 < len(texts):
            a.script(texts[index + 1], depth + 1, (*trace, f"payload of {cmd.prog} -c"))
        elif text.startswith("--command="):
            a.script(text.split("=", 1)[1], depth + 1, (*trace, f"payload of {cmd.prog}"))


def _rule_read_tool(a: _Analyzer, cmd: _Command, depth: int, trace: tuple[str, ...]) -> None:
    """Search and sort tools whose options name a program to run."""
    program_opts = {
        "rg": ("--pre", "--hostname-bin"),
        "sort": ("--compress-program",),
        "ag": ("--pager",),
        "man": ("--pager", "-P", "--html", "-H"),
    }[cmd.prog]
    args = cmd.args
    for index, word in enumerate(args):
        text = word.text
        if not word.quoted and word.dynamic and text.startswith("-"):
            stem = re.split(r"[{*?\[]", text, maxsplit=1)[0]
            if any(opt.startswith(stem) for opt in program_opts if len(stem) > 2):
                a.flag("dynamic_word", trace, f"{cmd.prog} option {text}")
        name, eq, value = text.partition("=")
        if name in program_opts:
            program = value if eq else (args[index + 1].text if index + 1 < len(args) else "")
            if program:
                a.script(program, depth + 1, (*trace, f"program of {cmd.prog} {name}"))


def _rule_sql_client(a: _Analyzer, cmd: _Command, depth: int, trace: tuple[str, ...]) -> None:
    parts = list(cmd.texts())
    for redirect in cmd.raw.redirects:
        if redirect.body:
            parts.append(redirect.body)
        if redirect.op == "<<<":
            parts.append(redirect.target.text)
    a.sql(" ; ".join(parts), trace)


def _rule_indus(a: _Analyzer, cmd: _Command, depth: int, trace: tuple[str, ...]) -> None:
    words = [t.lower() for t in cmd.texts() if not t.startswith("-")]
    if words[:1] == ["update"]:
        a.flag("indus_lifecycle", trace, "indus update")
    if words[:1] == ["gateway"] and len(words) > 1:
        if words[1] in ("stop", "restart"):
            a.flag("indus_lifecycle", trace, f"indus gateway {words[1]}")
        backgrounded = cmd.raw.op == "&" or bool({"nohup", "setsid"} & set(cmd.wrappers))
        if words[1] in ("run", "start") and backgrounded:
            a.flag("indus_lifecycle", trace, "indus gateway started in the background")


_RULES: dict[str, Rule] = {
    "rm": _rule_rm,
    "shred": _rule_shred,
    "srm": _rule_shred,
    "unlink": _rule_shred,
    "find": _rule_find,
    "xargs": _rule_xargs,
    "chmod": _rule_chmod,
    "chown": _rule_chown,
    "dd": _rule_dd,
    "tee": _rule_redirecting_writer,
    "cp": _rule_redirecting_writer,
    "mv": _rule_redirecting_writer,
    "install": _rule_redirecting_writer,
    "ln": _rule_redirecting_writer,
    "rsync": _rule_redirecting_writer,
    "sed": _rule_inplace,
    "perl": _rule_inplace,
    "ruby": _rule_inplace,
    "systemctl": _rule_systemctl,
    "service": _rule_service,
    "rc-service": _rule_service,
    "sv": _rule_service,
    "initctl": _rule_service,
    "launchctl": _rule_service,
    "shutdown": _rule_power,
    "reboot": _rule_power,
    "halt": _rule_power,
    "poweroff": _rule_power,
    "init": _rule_power,
    "telinit": _rule_power,
    "kill": _rule_kill,
    "pkill": _rule_pkill,
    "killall": _rule_pkill,
    "killall5": _rule_pkill,
    "git": _rule_git,
    "docker": _rule_docker,
    "podman": _rule_docker,
    "nerdctl": _rule_docker,
    "docker-compose": _rule_docker,
    "podman-compose": _rule_docker,
    "export": _rule_export,
    "eval": _rule_eval,
    "source": _rule_source,
    ".": _rule_source,
    "su": _rule_carrier,
    "runuser": _rule_carrier,
    "script": _rule_carrier,
    "watch": _rule_carrier,
    "rg": _rule_read_tool,
    "sort": _rule_read_tool,
    "ag": _rule_read_tool,
    "man": _rule_read_tool,
    "indus": _rule_indus,
}
for _name in FETCHERS:
    _RULES[_name] = _rule_fetcher
for _name in SQL_CLIENTS:
    _RULES[_name] = _rule_sql_client
for _name in _PACKAGE_REMOVE.keys() | {"pacman", "dpkg", "rpm", "npm", "pnpm", "yarn", "uv"}:
    _RULES[_name] = _rule_packages
for _name in (
    "fdisk",
    "sfdisk",
    "gdisk",
    "sgdisk",
    "parted",
    "wipefs",
    "blkdiscard",
    "diskutil",
    "cryptsetup",
    "badblocks",
):
    _RULES[_name] = _rule_disk


def _is_decoder(cmd: _Command) -> bool:
    texts = cmd.texts()
    letters, longs = _flags(texts)
    if cmd.prog in ("base64", "basenc", "base32"):
        return bool({"d", "D"} & letters) or "--decode" in longs
    if cmd.prog == "xxd":
        return "r" in letters or "--revert" in longs
    if cmd.prog == "openssl":
        return any(t in ("-d", "-decrypt", "--decrypt") for t in texts)
    if cmd.prog in ("tr", "rev", "uudecode", "gunzip", "zcat", "unxz", "bunzip2"):
        return True
    if cmd.prog in ("gzip", "bzip2", "xz", "zstd"):
        return "d" in letters or "--decompress" in longs
    if _is_python(cmd.prog) and texts[:2] == ["-m", "base64"]:
        return True
    if cmd.prog in ("printf", "echo"):
        return any(re.search(r"\\x[0-9a-fA-F]{2}|\\[0-7]{3}", t) for t in texts)
    return False


def _fed_text(before: Sequence[_Command | None]) -> list[str]:
    """Text earlier pipeline stages print: ``echo`` / ``printf`` arguments, here-documents."""
    out: list[str] = []
    for earlier in before:
        if earlier is None:
            continue
        if earlier.prog in ("echo", "printf"):
            out.append(" ".join(t for t in earlier.texts() if not re.fullmatch(r"-[neE]+", t)))
        for redirect in earlier.raw.redirects:
            if redirect.op in ("<<", "<<-") and redirect.body:
                out.append(redirect.body)
            elif redirect.op == "<<<":
                out.append(redirect.target.text)
    return out


def _uploads_stdin(cmd: _Command) -> bool:
    texts = cmd.texts()
    for index, text in enumerate(texts):
        value = texts[index + 1] if index + 1 < len(texts) else ""
        data = text in ("-d", "--data", "--data-binary", "--data-raw", "--data-urlencode", "--json")
        if data and (value.startswith("@-") or value == "@/dev/stdin"):
            return True
        if text in ("-T", "--upload-file") and value in ("-", "."):
            return True
        if text in ("-F", "--form") and value.endswith(("=@-", "=<-")):
            return True
        if text.startswith(("--post-file=", "--body-file=")) and text.endswith("/dev/stdin"):
            return True
        if re.fullmatch(r"-d@-", text):
            return True
    return False


# -- wrappers ------------------------------------------------------------------------------------

Unwrap = Callable[[_Analyzer, list[Word], int, tuple[str, ...]], list[Word] | None]


def _skip_options(
    words: list[Word],
    with_value: Iterable[str],
    *,
    stop_at: Iterable[str] = (),
    positionals: int = 0,
) -> list[Word] | None:
    """Drop leading options (and the values of ``with_value`` ones), then ``positionals`` words.

    Returns None when a non-executing option from ``stop_at`` appears.
    """
    takes = set(with_value)
    stops = set(stop_at)
    index = 0
    while index < len(words):
        text = words[index].text
        if text == "--":
            index += 1
            break
        if not text.startswith("-") or text == "-":
            break
        name = text.split("=", 1)[0]
        if name in stops:
            return None
        short_attached = len(text) > 2 and not text.startswith("--") and text[:2] in takes
        index += 2 if name in takes and "=" not in text and not short_attached else 1
    index += positionals
    return words[index:] if index <= len(words) else []


_SUDO_VALUE = tuple(
    [
        "-u",
        "--user",
        "-g",
        "--group",
        "-h",
        "--host",
        "-p",
        "--prompt",
        "-C",
        "--close-from",
        "-D",
        "--chdir",
        "-r",
        "--role",
        "-t",
        "--type",
        "-U",
        "--other-user",
        "-T",
        "--command-timeout",
    ]
)


def _unwrap_sudo(
    a: _Analyzer, words: list[Word], depth: int, trace: tuple[str, ...]
) -> list[Word] | None:
    for word in words:
        text = word.text
        if not text.startswith("-") or text == "--":
            break
        letters = _short_letters(text)
        if letters and text[:2] not in _SUDO_VALUE and set(letters) & set("SAsl"):
            a.flag("sudo_privilege", trace, f"sudo {text}")
        if text in ("--stdin", "--askpass", "--shell", "--list"):
            a.flag("sudo_privilege", trace, f"sudo {text}")
    return _skip_options(words, _SUDO_VALUE, stop_at=("-e", "--edit", "-v", "-k", "-K"))


def _unwrap_env(
    a: _Analyzer, words: list[Word], depth: int, trace: tuple[str, ...]
) -> list[Word] | None:
    index = 0
    while index < len(words):
        text = words[index].text
        if text in ("-S", "--split-string") and index + 1 < len(words):
            payload = " ".join(w.text for w in words[index + 1 :])
            a.script(payload, depth + 1, (*trace, "payload of env -S"))
            return None
        if text.startswith(("--split-string=", "-S")) and len(text) > 2 and text != "-S":
            payload = text.split("=", 1)[1] if text.startswith("--") else text[2:]
            rest = " ".join(w.text for w in words[index + 1 :])
            a.script(f"{payload} {rest}".strip(), depth + 1, (*trace, "payload of env -S"))
            return None
        if text in ("-u", "--unset", "-C", "--chdir", "-a", "--argv0"):
            index += 2
            continue
        if text.startswith("-") or _ASSIGNMENT.match(text):
            if text == "--":
                index += 1
                break
            index += 1
            continue
        break
    return words[index:]


def _plain(*with_value: str, stop_at: tuple[str, ...] = (), positionals: int = 0) -> Unwrap:
    def unwrap(
        a: _Analyzer, words: list[Word], depth: int, trace: tuple[str, ...]
    ) -> list[Word] | None:
        return _skip_options(words, with_value, stop_at=stop_at, positionals=positionals)

    return unwrap


def _unwrap_nice(
    a: _Analyzer, words: list[Word], depth: int, trace: tuple[str, ...]
) -> list[Word] | None:
    if words and re.fullmatch(r"-[+-]?\d+", words[0].text):
        words = words[1:]
    return _skip_options(words, ("-n", "--adjustment"))


def _unwrap_positional(*with_value: str, stop_at: tuple[str, ...] = ()) -> Unwrap:
    """Options, then one positional (a duration, priority, mask or new root), then the command."""
    return _plain(*with_value, stop_at=stop_at, positionals=1)


_WRAPPERS: dict[str, Unwrap] = {
    "sudo": _unwrap_sudo,
    "doas": _plain("-u", "-C"),
    "pkexec": _plain("--user"),
    "env": _unwrap_env,
    "exec": _plain("-a"),
    "nohup": _plain(),
    "setsid": _plain(),
    "time": _plain("-f", "--format", "-o", "--output"),
    "command": _plain(stop_at=("-v", "-V")),
    "builtin": _plain(),
    "nice": _unwrap_nice,
    "timeout": _unwrap_positional("-k", "--kill-after", "-s", "--signal"),
    "stdbuf": _plain("-i", "-o", "-e", "--input", "--output", "--error"),
    "ionice": _plain(
        "-c", "--class", "-n", "--classdata", stop_at=("-p", "--pid", "-P", "--pgid", "-u", "--uid")
    ),
    "chrt": _unwrap_positional("-T", "-P", "-D", stop_at=("-p", "--pid", "-m", "--max")),
    "taskset": _unwrap_positional(stop_at=("-p", "--pid")),
    "chroot": _unwrap_positional("--userspec", "--groups"),
    "busybox": _plain(),
    "caffeinate": _plain("-t", "-w"),
    "unbuffer": _plain(),
}


# -- entry ---------------------------------------------------------------------------------------


def _clean_home(home: str | None) -> str | None:
    if not home:
        return None
    text = home.rstrip("/")
    return text if text.count("/") >= 2 or (text.startswith("/") and len(text) > 1) else None


def _default_homes() -> tuple[str | None, str | None]:
    scope = scope_or_none()
    if scope is None:
        try:
            return str(Path.home()), None
        except RuntimeError:
            return None, None
    return scope.env.get("HOME") or None, str(scope.home)


def _show(raw: SimpleCommand) -> str:
    text = " ".join(w.text for w in raw.words)
    return text if len(text) <= 80 else text[:77] + "..."


def detect(
    command: str,
    *,
    user_home: str | None = None,
    indus_home: str | None = None,
) -> Detection:
    """Classify ``command``; homes default to the current scope's (``HOME`` and ``scope.home``)."""
    if user_home is None and indus_home is None:
        user_home, indus_home = _default_homes()
    analyzer = _Analyzer(user_home, indus_home)
    if _over_limits(command):
        return _verdict([_Finding("parser_limit", ("input over the size limits",))])
    if _hidden_chars(command):
        analyzer.findings.append(_Finding("hidden_unicode", ("hidden_unicode",)))
    try:
        analyzer.script(normalise(command), 0, ())
    except _Stop:
        pass
    except RecursionError:
        analyzer.findings.append(_Finding("parser_limit", ("recursion limit",)))
    return _verdict(analyzer.findings)


def _verdict(findings: list[_Finding]) -> Detection:
    if not findings:
        return SAFE
    hard = [f for f in findings if f.key in HARDLINE]
    chosen = hard[0] if hard else findings[0]
    return Detection(
        dangerous=True,
        hardline=bool(hard),
        pattern_key=chosen.key,
        description=DESCRIPTIONS[chosen.key],
        trace=chosen.trace,
    )
