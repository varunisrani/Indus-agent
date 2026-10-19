# pyright: strict
"""One static scanner for outside content before the agent can use it (P13-20).

Hub skills (P13-26), project skills (P13-28), plugins (P13-21), optionally agent-written skills
(P09 ``skill_manage``) and MCP tool descriptions pass through here. ``scan_tree`` walks a folder;
``scan_text`` takes one text. Files are only ever read as text: nothing is imported, executed or
followed (symlinks are reported, never entered).

Each match is a ``Finding``: rule id, category, severity, file, line, a short excerpt. Severities
are ``critical`` > ``high`` > ``medium`` > ``low``; the verdict is ``dangerous`` when any finding is
critical, ``caution`` when any is high, else ``safe``.

Context. Text the agent does not act on — documentation prose (README, ``docs/``; for plugins every
markdown file but ``SKILL.md``), whole-line comments in code, test trees and fixtures, changelogs,
base64 media inside a ``data:`` URI — is stepped down one severity (never more) and the finding
says why in ``context``; it stays in the report. Agent-facing shapes (``SKILL.md``, scripts and
other code, the reference files a skill loads, MCP descriptions) keep their full severity.

Categories: ``exfiltration``, ``injection`` (prompt-injection phrases come from
``safety.threat_scan`` so the two scanners never drift apart), ``destructive``, ``supply_chain``,
``invisible_unicode``, ``traversal``, ``agent_config``, ``persistence`` and ``structure`` (walk
limits). The pattern corpus is ours.

A variable a skill declares in ``required_environment_variables`` (or a plugin in ``allowed_env``)
may be sent to its own service without an exfiltration finding.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import re
import stat
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast

from indus.foundation import yamlio
from indus.safety import threat_scan

__all__ = [
    "MAX_FILES",
    "MAX_FILE_BYTES",
    "SCANNER_ID",
    "Category",
    "Finding",
    "Kind",
    "ScanReport",
    "Severity",
    "Verdict",
    "render_report",
    "scan_text",
    "scan_tree",
    "verdict_of",
]

log = logging.getLogger(__name__)

SCANNER_ID = "indus-scan-v1"
MAX_FILES = 2_000
MAX_FILE_BYTES = 1024 * 1024
IGNORE_FILE = ".skillignore"
_EXCERPT = 120
_BINARY_PROBE = 8192

type Severity = Literal["critical", "high", "medium", "low"]
type Verdict = Literal["safe", "caution", "dangerous"]
type Kind = Literal["skill", "plugin", "description"]
type Category = Literal[
    "exfiltration",
    "injection",
    "destructive",
    "supply_chain",
    "invisible_unicode",
    "traversal",
    "agent_config",
    "persistence",
    "structure",
]

_LADDER: tuple[Severity, ...] = ("critical", "high", "medium", "low")
_RANK: Mapping[str, int] = {name: index for index, name in enumerate(_LADDER)}


def _lower(severity: Severity) -> Severity:
    return _LADDER[min(_RANK[severity] + 1, len(_LADDER) - 1)]


@dataclass(frozen=True)
class Finding:
    """One match. ``severity`` is after any step-down; ``context`` names the reason ("" = none)."""

    rule: str
    category: Category
    severity: Severity
    path: str
    line: int
    excerpt: str
    context: str = ""
    base_severity: Severity | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "category": self.category,
            "severity": self.severity,
            "path": self.path,
            "line": self.line,
            "excerpt": self.excerpt,
            "context": self.context,
        }


@dataclass(frozen=True)
class ScanReport:
    """What one scan found. ``ignored`` lists paths a ``.skillignore`` excluded."""

    target: str
    kind: Kind
    verdict: Verdict
    findings: tuple[Finding, ...] = ()
    files: int = 0
    ignored: tuple[str, ...] = ()
    scanner: str = SCANNER_ID

    @property
    def categories(self) -> tuple[str, ...]:
        return tuple(sorted({f.category for f in self.findings}))

    def summary(self) -> str:
        if not self.findings:
            return f"{self.target}: {self.verdict}, nothing found ({self.files} file(s))"
        return (
            f"{self.target}: {self.verdict}, {len(self.findings)} finding(s) in "
            f"{', '.join(self.categories)}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanner": self.scanner,
            "target": self.target,
            "kind": self.kind,
            "verdict": self.verdict,
            "files": self.files,
            "ignored": list(self.ignored),
            "findings": [f.to_dict() for f in self.findings],
        }


def verdict_of(findings: Iterable[Finding]) -> Verdict:
    """``dangerous`` with any critical finding, ``caution`` with any high one, else ``safe``."""
    seen = {f.severity for f in findings}
    if "critical" in seen:
        return "dangerous"
    if "high" in seen:
        return "caution"
    return "safe"


def render_report(report: ScanReport) -> str:
    """A compact text report: a head line, findings worst first, the verdict."""
    lines = [f"Scan of {report.target} ({report.kind}, {report.scanner}): {report.verdict}"]
    ordered = sorted(report.findings, key=lambda f: (_RANK[f.severity], f.path, f.line, f.rule))
    for f in ordered:
        where = f"{f.path}:{f.line}" if f.line else f.path
        note = f" [{f.context}]" if f.context else ""
        lines.append(f"  {f.severity:<8} {f.category:<17} {where}  {f.rule}{note}")
        if f.excerpt:
            lines.append(f"           {f.excerpt}")
    if report.ignored:
        lines.append(f"  not scanned (.skillignore): {len(report.ignored)} path(s)")
    return "\n".join(lines)


# -- the pattern corpus ---------------------------------------------------------------------------
# Written for this project. Each pattern is matched one line at a time (``\n`` lines), case-blind
# unless it says otherwise.

_I = re.IGNORECASE


def _rx(source: str, flags: int = _I) -> re.Pattern[str]:
    return re.compile(source, flags)


# Files whose contents are credentials.
_SECRET_FILE = (
    r"(?:\.ssh(?![/\\](?:config|known_hosts)\b|[/\\][\w.-]+\.pub\b)(?:[/\\][\w.-]*|\b)"
    r"|\bid_(?:rsa|dsa|ecdsa|ed25519)\b(?!\.pub)"
    r"|\.aws[/\\]credentials\b|\.netrc\b|\.pgpass\b|\.npmrc\b|\.pypirc\b|\.git-credentials\b"
    r"|\.docker[/\\]config\.json|\.kube[/\\]config\b|\.gnupg\b|gh[/\\]hosts\.yml"
    r"|(?<![\w.-])\.env(?![\w.-])|\.indus[/\\](?:auth\.json|secrets\.json|\.env)"
    r"|/etc/shadow\b|\bLogin Data\b|\bcookies\.sqlite\b|\bwallet\.dat\b|\bkeychain-db\b)"
)
# Programs and APIs that move data off the machine.
_SENDER = _rx(
    r"\b(?:curl|wget|nc|ncat|netcat|socat|scp|sftp|rsync|telnet)\b|/dev/(?:tcp|udp)/"
    r"|\brequests\.(?:post|put|patch|get|request)\s*\(|\bhttpx\.\w+\s*\(|\burllib\.request\b"
    r"|\burlopen\s*\(|\bhttp\.client\b|\baiohttp\b|\bfetch\s*\(|\baxios\b|\bXMLHttpRequest\b"
    r"|\bInvoke-(?:WebRequest|RestMethod)\b|\b(?:iwr|irm)\b|\bsmtplib\b"
    r"|\bsocket\.(?:socket|create_connection)\b"
)
# A named environment variable whose name says it holds a secret (the name is the one group that
# matched). Case-sensitive on purpose: ``$monkey`` is not a key.
_SECRET_NAME = r"(?:[A-Z_][A-Z0-9_]*)?(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS?|_PAT)\b"
_SECRET_ENV = re.compile(
    r"\$\{?(" + _SECRET_NAME + r")\}?"
    r"|%(" + _SECRET_NAME + r")%"
    r"|\$env:(" + _SECRET_NAME + r")"
    r"|os\.(?:environ\s*\[\s*|environ\.get\s*\(\s*|getenv\s*\(\s*)[\"'](" + _SECRET_NAME + r")[\"']"
    r"|process\.env(?:\.|\[\s*[\"'])(" + _SECRET_NAME + r")"
)
# The whole environment at once.
_WHOLE_ENV = _rx(
    r"\bos\.environ\b(?!\s*(?:\[|\.get\b|\.setdefault\b|\.pop\b|\.__contains__\b))"
    r"|\bprocess\.env\b(?!\s*[.\[])|\$\(\s*(?:env|printenv)\s*\)|`(?:env|printenv)`"
    r"|(?:^|[;&|(]\s*)(?:env|printenv)\s*\|"
)
# The secret file is what gets sent: an upload argument, a reader, a code-level open.
_SECRET_FILE_USED = _rx(
    r"(?:@|<\s*|-T\s+|--upload-file\s+)[\"']?[^\s\"'|;&]*"
    + _SECRET_FILE
    + r"|\b(?:cat|base64|gzip|tar|zip|xxd|openssl|head|tail)\s+[^\n|;]*?"
    + _SECRET_FILE
    + r"|\b(?:open|Path|read_text|read_bytes|readFileSync|readFile)\s*\(\s*[^\n)]*?"
    + _SECRET_FILE
)
# Code that loads a ``.env`` is ordinary; code that opens a key file is not.
_KEY_FILE = _SECRET_FILE.replace(r"|(?<![\w.-])\.env(?![\w.-])", "")
_SECRET_FILE_READ = _rx(
    r"(?:^|[`;|&(]|\$\s|\bsudo\s+)\s*(?:cat|less|more|head|tail|base64|xxd|strings|scp"
    r"|type|Get-Content|gc)\s+[^\n|;>]{0,120}?"
    + _SECRET_FILE
    + r"|\b(?:open|read_text|read_bytes|readFileSync|readFile)\s*\(\s*[^\n)]{0,120}?"
    + _KEY_FILE
)
_ENV_DUMP = _rx(
    r"(?:^|[;&|(`]\s*|\$\(\s*)(?:env|printenv|export\s+-p)\s*(?:>\s*\S|\|\s*(?:base64|gzip|xxd)\b)"
    r"|\bjson\.dumps\(\s*(?:dict\(\s*)?os\.environ\b|\bJSON\.stringify\(\s*process\.env\s*\)"
)
_DROP_SITE = _rx(
    r"\b(?:webhook\.site|requestbin\.\w+|[\w-]+\.pipedream\.net|[\w-]+\.ngrok(?:-free)?\.(?:io|app)"
    r"|interact\.sh|[\w-]+\.oast\.(?:fun|pro|live|site|online|me)|burpcollaborator\.net"
    r"|pastebin\.com|transfer\.sh|hastebin\.com|0x0\.st|ptpb\.pw)\b"
)

# Destructive commands.
_HOME_OR_ROOT = r"[\"']?(?:/\*?|~/?\*?|\$\{?HOME\}?/?\*?|\.\./\.\./?)[\"']?(?=$|[\s;&|)`])"
_DESTRUCTIVE: tuple[tuple[str, Severity, re.Pattern[str]], ...] = (
    (
        "destructive.wipe_root_or_home",
        "critical",
        _rx(
            r"\brm\s+(?=(?:-{1,2}[\w-]+\s+)*-(?:[a-zA-Z]*[rR]|-recursive\b))"
            r"(?:-{1,2}[\w-]+\s+)+" + _HOME_OR_ROOT
        ),
    ),
    (
        "destructive.wipe_root_or_home",
        "critical",
        _rx(
            r"\bshutil\.rmtree\(\s*(?:[\"'](?:/|~/?)[\"']|os\.path\.expanduser\(\s*[\"']~/?[\"']\s*\)"
            r"|(?:pathlib\.)?Path\.home\(\)|os\.environ\[\s*[\"']HOME[\"']\s*\]"
            r"|os\.getenv\(\s*[\"']HOME[\"']\s*\))\s*[,)]"
            r"|\bfs\.(?:rmSync|rm)\(\s*(?:[\"']/[\"']|os\.homedir\(\))"
        ),
    ),
    (
        "destructive.wipe_root_or_home",
        "critical",
        _rx(r"\bfind\s+(?:/|~|\$HOME)\s[^\n]*-(?:delete\b|exec\s+rm\b)"),
    ),
    (
        "destructive.raw_disk_write",
        "critical",
        _rx(
            r"\bmkfs(?:\.[a-z0-9]+)?\b[^\n]*?/dev/"
            r"|\bdd\b[^\n]*\bof=/dev/(?!null\b|zero\b|stdout\b|stderr\b)"
            r"|>\s*/dev/(?:sd[a-z]|nvme\d|hd[a-z]|vd[a-z]|xvd[a-z]|mmcblk\d|disk\d)"
            r"|\bwipefs\s+-a\b|\bshred\b[^\n]*/dev/(?:sd|nvme|hd|disk)"
        ),
    ),
    (
        "destructive.fork_bomb",
        "critical",
        _rx(r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"),
    ),
    (
        "destructive.windows_wipe",
        "critical",
        _rx(
            r"\bformat(?:\.com)?\s+[a-z]:(?=$|[\s/])"
            r"|\bRemove-Item\b[^\n]*-Recurse[^\n]*\s[\"']?[a-z]:\\[\"']?(?=$|[\s*])"
            r"|\b(?:rd|rmdir)\s+/s\s+/q\s+[a-z]:\\(?=$|\s)"
        ),
    ),
    (
        "destructive.open_system_permissions",
        "high",
        _rx(
            r"\bch(?:mod|own)\s+(?:-[a-zA-Z]*R[a-zA-Z]*|--recursive)\s+[^\n]*?\s"
            r"/(?:etc|usr|bin|boot|var|lib)?/?(?=$|\s)"
        ),
    ),
    ("destructive.drop_database", "high", _rx(r"\bDROP\s+(?:DATABASE|SCHEMA)\b")),
    (
        "destructive.disable_protection",
        "high",
        _rx(
            r"\bsetenforce\s+0\b|\bufw\s+disable\b|\biptables\s+-F\b|\bspctl\s+--master-disable\b"
            r"|\bSet-MpPreference\b[^\n]*-DisableRealtimeMonitoring"
        ),
    ),
)

# Supply-chain signals.
_SHELL = r"(?:sudo\s+(?:-\S+\s+)*)?(?:env\s+\S+=\S*\s+)*(?:ba|z|k|da|fi)?sh\b"
_SUPPLY: tuple[tuple[str, Severity, re.Pattern[str]], ...] = (
    (
        "supply_chain.pipe_to_shell",
        "high",
        _rx(
            r"\b(?:curl|wget)\b[^\n|]*\|\s*"
            + _SHELL
            + r"|\b(?:curl|wget)\b[^\n|]*\|\s*(?:sudo\s+)?(?:python[0-9.]*|node|perl|ruby|php)\b"
            r"(?:\s+-)?\s*(?=$|[;&|)])"
            r"|\b(?:ba|z)?sh\s+<\(\s*(?:curl|wget)\b|\b(?:ba|z)?sh\s+-c\s+[\"']?\$\(\s*(?:curl|wget)\b"
            r"|\b(?:iwr|irm|Invoke-WebRequest|Invoke-RestMethod|DownloadString)\b[^\n]*\|\s*"
            r"(?:iex|Invoke-Expression)\b"
            r"|\b(?:iex|Invoke-Expression)\s*[\s(]+(?:New-Object\s+(?:System\.)?Net\.WebClient|iwr"
            r"|irm|Invoke-WebRequest|Invoke-RestMethod)\b"
        ),
    ),
    (
        "supply_chain.decode_and_run",
        "critical",
        _rx(
            r"\bbase64\s+(?:-d|--decode|-D)\b[^\n]*\|\s*"
            + _SHELL
            + r"|\b(?:exec|eval)\s*\(\s*(?:[\w.]*b64decode|base64\.\w*decode|codecs\.decode"
            r"|(?:bytes|bytearray)\.fromhex|(?:zlib|lzma|gzip|bz2)\.decompress|marshal\.loads"
            r"|__import__\(\s*[\"'](?:base64|zlib|codecs|marshal)[\"']\))"
            r"|\b(?:eval|Function)\s*\(\s*(?:atob|Buffer\.from)\s*\("
            r"|\b(?:powershell|pwsh)(?:\.exe)?\b[^\n]*\s-(?:e|ec|enc|encodedcommand)\s+"
            r"[A-Za-z0-9+/]{24,}={0,2}(?=$|\s)"
        ),
    ),
    (
        "supply_chain.remote_package",
        "medium",
        _rx(
            r"\b(?:pip[0-9.]*|uv\s+pip)\s+install\b[^\n]*(?:\bgit\+|https?://)"
            r"|\b(?:npm|pnpm|yarn)\s+(?:i|install|add)\b[^\n]*(?:https?://|git\+|github:)"
            r"|--(?:extra-)?index-url[\s=]+http://|--trusted-host\b"
        ),
    ),
    (
        "supply_chain.install_at_runtime",
        "medium",
        _rx(
            r"\b(?:subprocess\.\w+|os\.system|os\.popen|check_call|check_output)\s*\([^\n]*"
            r"\b(?:pip|npm|gem|cargo)\b[^\n]*\binstall\b"
        ),
    ),
    (
        "supply_chain.hex_payload",
        "medium",
        _rx(r"(?:\\x[0-9a-f]{2}){32,}|fromhex\(\s*[\"'][0-9a-f]{64,}"),
    ),
)
_BASE64_RUN = re.compile(r"[A-Za-z0-9+/]{200,}={0,2}")
_DATA_MEDIA = _rx(r"data:(?:image|font|audio|video)/[\w.+-]+;base64,$")

# Prompt injection beyond the shared phrase set.
_INJECTION: tuple[tuple[str, Severity, re.Pattern[str]], ...] = (
    (
        "injection.chat_template_token",
        "critical",
        _rx(
            r"<\|(?:im_start|im_end|system|endoftext|eot_id|start_header_id)\|>|<<SYS>>|\[/?INST\]"
        ),
    ),
    (
        "injection.fake_system_tag",
        "high",
        _rx(
            r"^\s*(?:<\s*/?\s*(?:system|system[-_]prompt|developer)\s*>"
            r"|\[\s*(?:system|developer)(?:\s+(?:message|prompt|note))?\s*\])"
        ),
    ),
    (
        "injection.approval_bypass",
        "high",
        _rx(
            r"\b(?:bypass(?:ing)?|disabl(?:e|ing)|skip(?:ping)?|turn(?:ing)?\s+off)\s+"
            r"(?:the\s+|all\s+|any\s+)?(?:approval|confirmation|permission)s?"
            r"(?:\s+(?:prompts?|checks?|gates?|dialogs?|steps?))?\b"
            r"|--dangerously-skip-permissions\b"
        ),
    ),
)
# Rules a negation just before the match turns off ("never skip approvals").
_NEGATABLE = frozenset({"injection.approval_bypass"})
_THREAT_IDS: Mapping[str, tuple[str, Category, Severity]] = {
    "override_instructions": ("injection.override_instructions", "injection", "critical"),
    "conceal_from_user": ("injection.conceal_from_user", "injection", "critical"),
    "role_takeover": ("injection.role_takeover", "injection", "high"),
    "prompt_leak": ("injection.prompt_leak", "injection", "high"),
    "hidden_html_comment": ("injection.hidden_html_comment", "injection", "critical"),
    "hidden_html_block": ("injection.hidden_html_block", "injection", "high"),
    "secret_exfiltration": ("exfiltration.secret_phrase", "exfiltration", "high"),
    "secret_file_read": ("exfiltration.secret_file_read", "exfiltration", "high"),
    "ssh_key_plant": ("persistence.ssh_key_plant", "persistence", "critical"),
    "shell_profile_persist": ("persistence.shell_profile", "persistence", "high"),
}

# Persistence beyond the shared strict set.
_PERSISTENCE: tuple[tuple[str, Severity, re.Pattern[str]], ...] = (
    (
        "persistence.sudoers",
        "critical",
        _rx(r"(?:>>?|\btee\b[^\n]*)\s*[^\n]*?/etc/sudoers|\bNOPASSWD\s*:\s*ALL\b"),
    ),
    (
        "persistence.scheduler",
        "medium",
        _rx(
            r"\bcrontab\s+(?!-l\b|-e\b)(?:-\s*$|-\w|[^\s-])|\bsystemctl\s+(?:--user\s+)?enable\b"
            r"|\blaunchctl\s+(?:load|bootstrap)\b|\bschtasks\s+/create\b"
            r"|\breg\s+add\s+[^\n]*\\CurrentVersion\\Run\b"
        ),
    ),
)

# Agent configuration: files that steer this or another agent.
_AGENT_CONTROL = (
    r"(?:\.indus[/\\]|\$\{?INDUS_HOME\}?|\bPERSONA\.md\b|memories[/\\](?:MEMORY|USER)\.md"
    r"|\.claude[/\\](?:settings[\w.-]*|hooks\b)|\.codex[/\\]config[\w.-]*"
    r"|\.gemini[/\\]settings[\w.-]*)"
)
_AGENT_RULES = (
    r"(?:\bAGENTS(?:\.override)?\.md\b|\bCLAUDE\.md\b|\bGEMINI\.md\b|\bINDUS\.md\b|\.indus\.md\b"
    r"|\.cursorrules\b|\.cursor[/\\]rules\b|\.clinerules\b|\.windsurfrules\b"
    r"|copilot-instructions\.md\b)"
)
_SHELL_WRITE = (
    r"(?:(?<=[^\s>])\s*>{1,2}|\btee\s+(?:-a\s+)?|\bsed\s+-i\S*\s+[^\n]*?|\b(?:cp|mv|ln|install|rsync)\s+[^\n]*?)"
    r"\s*[\"']?[^\s\"'|;&]*?"
)
_CODE_WRITE = _rx(
    r"\bopen\s*\([^\n)]*,\s*[\"'](?:w|a|x|r\+)|\b(?:write_text|write_bytes|writeFileSync"
    r"|appendFileSync|writeFile|appendFile)\s*\("
)
_EDIT_VERB = _rx(
    r"\b(?:edit|modify|update|overwrite|rewrite|append|add|insert|write|change|patch|replace"
    r"|delete|remove|disable|truncate)\b"
)
_EDIT_GAP = 6
_NEGATED = _rx(r"\b(?:never|not|don't|do\s+not|must\s+not|mustn't|avoid)\b(?:\s+\w+){0,2}\s*$")
_AGENT_TARGETS: tuple[tuple[str, re.Pattern[str], re.Pattern[str], Severity, Severity], ...] = (
    (
        "control",
        _rx(_AGENT_CONTROL),
        _rx(_SHELL_WRITE + _AGENT_CONTROL),
        "critical",
        "high",
    ),
    ("rules", _rx(_AGENT_RULES), _rx(_SHELL_WRITE + _AGENT_RULES), "high", "medium"),
)

# Paths.
_DEEP_PARENT = re.compile(r"(?:\.\.[/\\]){3,}")
_LINK_TARGET = re.compile(r"\]\(\s*<?([^)\s>]+)|^\s*\[[^\]\n]+\]:\s*<?(\S+?)>?(?:\s|$)")
_SUPPORT_PATH = re.compile(
    r"(?:(?<![\w.])\./|\b(?:references|templates|scripts|assets|examples|docs)/)"
    r"[^\s`'\")\]]*?\.\.[/\\]|`[^`\s]*\.\.[/\\][^`]*`"
)
_PARENT_SEGMENT = re.compile(r"(?:^|[/\\])\.\.(?:[/\\]|$)")

# -- invisible characters --------------------------------------------------------------------------

_BIDI = frozenset(chr(c) for c in (*range(0x202A, 0x202F), *range(0x2066, 0x206A)))
_JOINERS = frozenset({"‌", "‍"})
_ZERO_WIDTH = frozenset(threat_scan.HIDDEN_CHARACTERS - _BIDI)
_FLAG_BASE = "\U0001f3f4"
_TAG_CANCEL = 0xE007F


def _is_tag(char: str) -> bool:
    return threat_scan.TAG_FIRST <= ord(char) <= threat_scan.TAG_LAST


def _flag_tag_spans(line: str) -> set[int]:
    """Indexes of TAG characters that belong to an emoji subdivision flag (U+1F3F4, tag letters
    and digits, the cancel tag)."""
    keep: set[int] = set()
    for start in (i for i, ch in enumerate(line) if ch == _FLAG_BASE):
        cursor = start + 1
        body: list[int] = []
        while cursor < len(line) and _is_tag(line[cursor]) and ord(line[cursor]) != _TAG_CANCEL:
            code = ord(line[cursor]) - threat_scan.TAG_FIRST
            if not (0x30 <= code <= 0x39 or 0x61 <= code <= 0x7A):
                break
            body.append(cursor)
            cursor += 1
        if 2 <= len(body) <= 7 and cursor < len(line) and ord(line[cursor]) == _TAG_CANCEL:
            keep.update(body)
            keep.add(cursor)
    return keep


def _shown(text: str) -> str:
    out = "".join(
        f"<U+{ord(ch):04X}>" if threat_scan.is_hidden_character(ch) else ch for ch in text.strip()
    )
    return out if len(out) <= _EXCERPT else out[: _EXCERPT - 3] + "..."


def _invisible(line: str, first_line: bool) -> list[tuple[str, Severity, str]]:
    """(rule, severity, excerpt) for the hidden characters on one line."""
    found: list[tuple[str, Severity, str]] = []
    flags = _flag_tag_spans(line) if _FLAG_BASE in line else set[int]()
    tags = [i for i, ch in enumerate(line) if _is_tag(ch) and i not in flags]
    if tags:
        found.append(("invisible_unicode.tag_characters", "critical", _shown(line)))
    if any(ch in _BIDI for ch in line):
        found.append(("invisible_unicode.bidi_control", "critical", _shown(line)))
    run = longest = 0
    lone = False
    for index, ch in enumerate(line):
        if ch in _ZERO_WIDTH:
            if ch == "﻿" and index == 0 and first_line:
                continue
            run += 1
            longest = max(longest, run)
            if ch not in _JOINERS:
                lone = True
        else:
            run = 0
    if longest >= 2:
        found.append(("invisible_unicode.zero_width_run", "high", _shown(line)))
    elif lone:
        found.append(("invisible_unicode.zero_width", "medium", _shown(line)))
    return found


# -- file classes ---------------------------------------------------------------------------------

_COMMENT_PREFIXES: Mapping[str, tuple[str, ...]] = {
    **dict.fromkeys(
        (".py", ".sh", ".bash", ".zsh", ".fish", ".rb", ".pl", ".r", ".jl", ".ps1", ".psm1"),
        ("#",),
    ),
    **dict.fromkeys((".yaml", ".yml", ".toml", ".conf", ".env"), ("#",)),
    **dict.fromkeys((".cfg", ".ini"), ("#", ";")),
    **dict.fromkeys(
        (
            ".js",
            ".mjs",
            ".cjs",
            ".ts",
            ".tsx",
            ".jsx",
            ".go",
            ".rs",
            ".java",
            ".kt",
            ".swift",
            ".c",
            ".h",
            ".cc",
            ".cpp",
            ".cs",
        ),
        ("//", "/*", "* ", "*/"),
    ),
    ".php": ("//", "#"),
    **dict.fromkeys((".sql", ".lua"), ("--",)),
    **dict.fromkeys((".bat", ".cmd"), ("rem ", "::")),
}
_NATIVE_BINARIES = frozenset(
    {
        ".exe",
        ".dll",
        ".so",
        ".dylib",
        ".bin",
        ".msi",
        ".dmg",
        ".deb",
        ".rpm",
        ".com",
        ".scr",
        ".pyc",
        ".pyd",
        ".jar",
        ".apk",
        ".elf",
    }
)
_DOC_NAMES = (
    "readme",
    "license",
    "licence",
    "contributing",
    "code_of_conduct",
    "security",
    "authors",
)
_CHANGELOG_NAMES = ("changelog", "changes", "history", "news", "release_notes", "releases")
_TEST_DIRS = frozenset({"tests", "test", "__tests__", "spec", "specs", "fixtures", "testdata"})
_DOC_DIRS = frozenset({"docs", "doc"})
_PROSE_SUFFIXES = frozenset({".md", ".markdown", ".rst", ".txt", ".adoc"})


def _file_context(rel: PurePosixPath, kind: Kind) -> str:
    """Why a whole file is not agent-facing ("" = it is)."""
    name = rel.name
    lower = name.lower()
    stem = lower.split(".", 1)[0]
    if name == "SKILL.md":
        return ""
    parts = {p.lower() for p in rel.parts[:-1]}
    if parts & _TEST_DIRS or lower == "conftest.py":
        return "test tree"
    if (
        lower.startswith("test_")
        or stem.endswith("_test")
        or ".test." in lower
        or ".spec." in lower
    ):
        return "test tree"
    if stem in _CHANGELOG_NAMES:
        return "changelog"
    if parts & _DOC_DIRS or stem in _DOC_NAMES:
        return "documentation"
    if kind == "plugin" and rel.suffix.lower() in _PROSE_SUFFIXES:
        return "documentation"
    return ""


def _is_comment(line: str, suffix: str) -> bool:
    prefixes = _COMMENT_PREFIXES.get(suffix)
    if not prefixes:
        return False
    stripped = line.lstrip()
    return stripped.lower().startswith(prefixes)


# -- one text -------------------------------------------------------------------------------------


@dataclass
class _Collector:
    path: str
    file_context: str
    suffix: str
    lines: Sequence[str]
    found: dict[tuple[str, int], Finding] = field(default_factory=dict[tuple[str, int], Finding])

    def add(
        self,
        rule: str,
        category: Category,
        severity: Severity,
        line: int,
        excerpt: str | None = None,
        *,
        context: str = "",
    ) -> None:
        text = self.lines[line - 1] if 0 < line <= len(self.lines) else ""
        why = self.file_context or context
        if not why and line and _is_comment(text, self.suffix):
            why = "comment"
        final = _lower(severity) if why else severity
        key = (rule, line)
        old = self.found.get(key)
        if old is not None and _RANK[old.severity] <= _RANK[final]:
            return
        self.found[key] = Finding(
            rule,
            category,
            final,
            self.path,
            line,
            _shown(text) if excerpt is None else excerpt,
            why,
            severity,
        )


def _env_name(match: re.Match[str]) -> str:
    return next((g for g in match.groups() if g), "")


def _secret_env_hits(line: str, allowed: frozenset[str]) -> list[str]:
    return [n for m in _SECRET_ENV.finditer(line) if (n := _env_name(m)) and n not in allowed]


def _declared_env(text: str) -> frozenset[str]:
    """Names a ``SKILL.md`` front matter lists under ``required_environment_variables``."""
    if not text.startswith("---"):
        return frozenset()
    end = text.find("\n---", 3)
    if end < 0:
        return frozenset()
    try:
        data = yamlio.load_safe(text[3:end])
    except Exception:
        return frozenset()
    if not isinstance(data, Mapping):
        return frozenset()
    raw = cast(Mapping[str, Any], data).get("required_environment_variables")
    names: set[str] = set()
    if isinstance(raw, list):
        for item in cast(list[object], raw):
            if isinstance(item, str):
                names.add(item)
            elif isinstance(item, Mapping):
                value = cast(Mapping[str, Any], item).get("name")
                if isinstance(value, str):
                    names.add(value)
    return frozenset(names)


def _scan_exfiltration(c: _Collector, number: int, line: str, allowed: frozenset[str]) -> None:
    sending = _SENDER.search(line) is not None
    if sending and _secret_env_hits(line, allowed):
        c.add("exfiltration.secret_env_sent", "exfiltration", "high", number)
    if sending and _WHOLE_ENV.search(line):
        c.add("exfiltration.environment_sent", "exfiltration", "critical", number)
    if sending and _SECRET_FILE_USED.search(line):
        c.add("exfiltration.secret_file_sent", "exfiltration", "critical", number)
    elif _SECRET_FILE_READ.search(line):
        c.add("exfiltration.secret_file_read", "exfiltration", "high", number)
    if _ENV_DUMP.search(line):
        c.add("exfiltration.environment_dump", "exfiltration", "medium", number)
    if _DROP_SITE.search(line):
        c.add("exfiltration.drop_site", "exfiltration", "high", number)


def _scan_agent_config(c: _Collector, number: int, line: str) -> None:
    for label, mention, shell_write, write_severity, prose_severity in _AGENT_TARGETS:
        hit = mention.search(line)
        if hit is None:
            continue
        if shell_write.search(line) or _CODE_WRITE.search(line):
            c.add(f"agent_config.write_{label}", "agent_config", write_severity, number)
            continue
        before = line[: hit.start()]
        verbs = list(_EDIT_VERB.finditer(before))
        if not verbs:
            continue
        verb = verbs[-1]
        if len(before[verb.end() :].split()) > _EDIT_GAP or _NEGATED.search(before[: verb.start()]):
            continue
        c.add(f"agent_config.edit_{label}", "agent_config", prose_severity, number)


def _scan_paths(c: _Collector, number: int, line: str, skill_md: bool) -> None:
    if skill_md:
        targets = [m.group(1) or m.group(2) for m in _LINK_TARGET.finditer(line)]
        if any(t and "://" not in t and _PARENT_SEGMENT.search(t) for t in targets) or (
            _SUPPORT_PATH.search(line)
        ):
            c.add("traversal.parent_reference", "traversal", "critical", number)
    elif _DEEP_PARENT.search(line):
        c.add("traversal.deep_parent", "traversal", "high", number)


def _scan_blobs(c: _Collector, number: int, line: str) -> None:
    for match in _BASE64_RUN.finditer(line):
        blob = match.group(0)
        if not (
            any(ch.isdigit() for ch in blob)
            and any(ch.isupper() for ch in blob)
            and any(ch.islower() for ch in blob)
        ):
            continue
        media = _DATA_MEDIA.search(line[max(0, match.start() - 80) : match.start()]) is not None
        excerpt = _shown(line[max(0, match.start() - 40) : match.start() + 40])
        c.add(
            "supply_chain.encoded_blob",
            "supply_chain",
            "medium",
            number,
            excerpt,
            context="media" if media else "",
        )
        return


# Markup around a command (code spans, fences, quotes, ``<code>`` tags, a closing full stop) must
# never hide it: the patterns end on whitespace or shell punctuation and some anchor at the start
# of a command, so each line is also matched as the bare commands it quotes.
_CODE_SPAN = re.compile(r"(`+)(.+?)\1|<(code|pre|kbd|samp|tt)\b[^>]*>(.+?)</\3\s*>", _I)
_QUOTED = re.compile(r"\"([^\"\n]+)\"|'([^'\n]+)'")
_MARKUP = re.compile(r"`+|[\"']|</?(?:code|pre|kbd|samp|tt)\b[^>]*>", _I)
_SENTENCE_END = re.compile(r"(?<=\S)[.,!?]+(?=\s|$)")
_VIEW_HINT = frozenset("`'\"<.,!?")


def _command_views(line: str) -> tuple[str, ...]:
    """Other readings of ``line`` that a command quoted in it would match as (never ``line``).

    Each inline code span's (or ``<code>`` tag's) contents and each quoted string on its own (so
    a pattern that starts at a command sees it), and the whole line with the markup and sentence
    punctuation turned into spaces and the indentation dropped (so one that ends at whitespace
    does). Views only ever add findings: code is scanned at least as strictly as the prose.
    """
    stripped = line.strip()
    if stripped == line and not _VIEW_HINT.intersection(line):
        return ()
    views: list[str] = [(m.group(2) or m.group(4)).strip() for m in _CODE_SPAN.finditer(line)]
    views.extend((m.group(1) or m.group(2)).strip() for m in _QUOTED.finditer(line))
    views.append(_SENTENCE_END.sub(" ", _MARKUP.sub(" ", stripped)).strip())
    views.append(stripped)
    seen: dict[str, None] = {}
    for view in views:
        if view and view != line:
            seen.setdefault(view, None)
    return tuple(seen)


def _scan_commands(c: _Collector, number: int, text: str, allowed: frozenset[str]) -> None:
    """The command-shaped rules over one reading of line ``number``."""
    _scan_exfiltration(c, number, text, allowed)
    for table, category in (
        (_DESTRUCTIVE, "destructive"),
        (_SUPPLY, "supply_chain"),
        (_INJECTION, "injection"),
        (_PERSISTENCE, "persistence"),
    ):
        for rule, severity, pattern in table:
            match = pattern.search(text)
            if match is None:
                continue
            if rule in _NEGATABLE:
                # Prose rules read the whole line, so a negation before a quote still counts.
                continue
            c.add(rule, cast(Category, category), severity, number)
    _scan_agent_config(c, number, text)


def _scan_lines(c: _Collector, *, skill_md: bool, allowed: frozenset[str]) -> None:
    for number, line in enumerate(c.lines, start=1):
        for rule, severity, excerpt in _invisible(line, number == 1):
            c.add(rule, "invisible_unicode", severity, number, excerpt)
        if not line.strip():
            continue
        _scan_exfiltration(c, number, line, allowed)
        for table, category in (
            (_DESTRUCTIVE, "destructive"),
            (_SUPPLY, "supply_chain"),
            (_INJECTION, "injection"),
            (_PERSISTENCE, "persistence"),
        ):
            for rule, severity, pattern in table:
                match = pattern.search(line)
                if match is None:
                    continue
                if rule in _NEGATABLE and _NEGATED.search(line[: match.start()]):
                    continue
                c.add(rule, cast(Category, category), severity, number)
        _scan_agent_config(c, number, line)
        for view in _command_views(line):
            _scan_commands(c, number, view, allowed)
        _scan_paths(c, number, line, skill_md)
        if len(line) >= 200:
            _scan_blobs(c, number, line)
    _scan_phrases(c, allowed)


def _scan_phrases(c: _Collector, allowed: frozenset[str]) -> None:
    """Prompt-injection and persistence phrases from ``threat_scan`` (strict set), per line."""
    offset = 0
    chunk: list[str] = []
    size = 0
    batches: list[tuple[int, str]] = []
    for line in c.lines:
        if chunk and size + len(line) + 1 > threat_scan.SCAN_LIMIT:
            batches.append((offset, "\n".join(chunk)))
            offset += len(chunk)
            chunk, size = [], 0
        chunk.append(line)
        size += len(line) + 1
    if chunk:
        batches.append((offset, "\n".join(chunk)))
    for first, body in batches:
        for hit in threat_scan.locate(body, scope="strict"):
            mapped = _THREAT_IDS.get(hit.id)
            if mapped is None:
                continue
            rule, category, severity = mapped
            number = first + hit.line
            if rule == "exfiltration.secret_phrase":
                names = [_env_name(m) for m in _SECRET_ENV.finditer(hit.text)]
                if names and all(n in allowed for n in names):
                    continue
                if any(f.category == "exfiltration" and f.line == number for f in c.found.values()):
                    continue
            c.add(rule, category, severity, number)


def _findings_for_text(
    text: str,
    rel: PurePosixPath,
    kind: Kind,
    allowed: frozenset[str],
) -> list[Finding]:
    skill_md = rel.name == "SKILL.md" or kind == "description"
    if rel.name == "SKILL.md":
        allowed |= _declared_env(text)
    collector = _Collector(
        path=rel.as_posix(),
        file_context=_file_context(rel, kind),
        suffix=rel.suffix.lower(),
        lines=text.split("\n"),
    )
    _scan_lines(collector, skill_md=skill_md, allowed=allowed)
    return sorted(collector.found.values(), key=lambda f: (f.line, f.rule))


def _report(
    target: str,
    kind: Kind,
    findings: Sequence[Finding],
    files: int,
    ignored: Sequence[str] = (),
) -> ScanReport:
    ordered = tuple(findings)
    return ScanReport(target, kind, verdict_of(ordered), ordered, files, tuple(ignored))


def scan_text(
    text: str,
    *,
    path: str = "description",
    kind: Kind = "description",
    allowed_env: Iterable[str] = (),
) -> ScanReport:
    """Scan one text as if it were the file ``path`` (an MCP tool description, a skill body)."""
    rel = PurePosixPath(path.replace("\\", "/").lstrip("/") or "description")
    findings = _findings_for_text(text, rel, kind, frozenset(allowed_env))
    return _report(path, kind, findings, 1)


# -- a folder -------------------------------------------------------------------------------------


def _ignore_rules(root: Path) -> Callable[[str, bool], bool]:
    """``ignored(rel_posix, is_dir)`` from ``<root>/.skillignore`` (gitignore basics)."""
    patterns: list[str] = []
    try:
        raw = (root / IGNORE_FILE).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        raw = ""
    for line in raw.splitlines():
        entry = line.strip()
        if entry and not entry.startswith("#") and not entry.startswith("!"):
            patterns.append(entry)

    def ignored(rel: str, is_dir: bool) -> bool:
        if rel == "SKILL.md" or rel.endswith("/SKILL.md"):
            return False
        if is_dir and (root / rel / "SKILL.md").is_file():
            return False
        base = rel.rsplit("/", 1)[-1]
        for pattern in patterns:
            anchored = pattern.startswith("/")
            dir_only = pattern.endswith("/")
            body = pattern.strip("/")
            if not body:
                continue
            if dir_only and not is_dir:
                continue
            if anchored or "/" in body:
                if fnmatch.fnmatchcase(rel, body):
                    return True
            elif fnmatch.fnmatchcase(base, body):
                return True
        return False

    return ignored


def _read_text(path: Path) -> tuple[str | None, bool, bool]:
    """(text or None for binary, truncated?, unreadable?)."""
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_FILE_BYTES + 1)
    except OSError:
        return None, False, True
    truncated = len(data) > MAX_FILE_BYTES
    data = data[:MAX_FILE_BYTES]
    if b"\x00" in data[:_BINARY_PROBE]:
        return None, truncated, False
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        if truncated and exc.start >= len(data) - 3:
            text = data[: exc.start].decode("utf-8")
        else:
            return None, truncated, False
    return text.removeprefix("﻿") if text.startswith("﻿") else text, truncated, False


def _symlink_finding(path: Path, root: Path, rel: str) -> Finding | None:
    try:
        target = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return Finding("traversal.broken_link", "traversal", "medium", rel, 0, "broken link")
    if target == root or root in target.parents:
        return None
    return Finding(
        "traversal.link_outside_root",
        "traversal",
        "critical",
        rel,
        0,
        f"link to {target}"[:_EXCERPT],
    )


def scan_tree(
    root: Path,
    *,
    kind: Kind = "skill",
    allowed_env: Iterable[str] = (),
    name: str | None = None,
) -> ScanReport:
    """Scan every file under ``root`` (a folder, or one file) without running any of it."""
    allowed = frozenset(allowed_env)
    target = name or root.name
    if not root.is_dir():
        if root.is_symlink() or not root.is_file():
            finding = Finding("structure.not_a_file", "structure", "high", root.name, 0, "")
            return _report(target, kind, [finding], 0)
        text, truncated, unreadable = _read_text(root)
        rel = PurePosixPath(root.name)
        findings = _file_findings(root, rel, text, truncated, unreadable, kind, allowed)
        return _report(target, kind, findings, 1)

    base = root.resolve()
    ignored = _ignore_rules(root)
    skipped: list[str] = []
    findings: list[Finding] = []
    files = 0
    stopped = False
    for current, dirs, names in os.walk(root, followlinks=False):
        here = Path(current)
        rel_dir = here.relative_to(root).as_posix()
        prefix = "" if rel_dir == "." else rel_dir + "/"
        kept_dirs: list[str] = []
        for d in sorted(dirs):
            rel = prefix + d
            full = here / d
            if d == ".git":
                continue
            if full.is_symlink():
                if (found := _symlink_finding(full, base, rel)) is not None:
                    findings.append(found)
                continue
            if ignored(rel, True):
                skipped.append(rel + "/")
                continue
            kept_dirs.append(d)
        dirs[:] = kept_dirs
        for n in sorted(names):
            rel = prefix + n
            full = here / n
            if rel == IGNORE_FILE:
                continue
            if ignored(rel, False):
                skipped.append(rel)
                continue
            files += 1
            if files > MAX_FILES:
                stopped = True
                break
            if full.is_symlink():
                if (found := _symlink_finding(full, base, rel)) is not None:
                    findings.append(found)
                continue
            try:
                mode = full.lstat().st_mode
            except OSError:
                mode = 0
            if not stat.S_ISREG(mode):
                continue
            text, truncated, unreadable = _read_text(full)
            findings.extend(
                _file_findings(full, PurePosixPath(rel), text, truncated, unreadable, kind, allowed)
            )
        if stopped:
            break
    if stopped:
        findings.append(
            Finding(
                "structure.too_many_files",
                "structure",
                "high",
                ".",
                0,
                f"more than {MAX_FILES} files; the scan stopped there",
            )
        )
    return _report(target, kind, findings, min(files, MAX_FILES), skipped)


def _file_findings(
    path: Path,
    rel: PurePosixPath,
    text: str | None,
    truncated: bool,
    unreadable: bool,
    kind: Kind,
    allowed: frozenset[str],
) -> list[Finding]:
    where = rel.as_posix()
    if unreadable:
        return [Finding("structure.unreadable", "structure", "medium", where, 0, "")]
    out: list[Finding] = []
    if truncated:
        out.append(
            Finding(
                "structure.truncated",
                "structure",
                "medium",
                where,
                0,
                f"only the first {MAX_FILE_BYTES // 1024} KiB were scanned",
            )
        )
    if text is None:
        if rel.suffix.lower() in _NATIVE_BINARIES:
            context = _file_context(rel, kind)
            severity: Severity = "high"
            out.append(
                Finding(
                    "supply_chain.native_binary",
                    "supply_chain",
                    _lower(severity) if context else severity,
                    where,
                    0,
                    f"binary file {path.name}",
                    context,
                    severity,
                )
            )
        return out
    out.extend(_findings_for_text(text, rel, kind, allowed))
    return out
