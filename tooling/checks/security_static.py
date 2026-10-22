"""Security static guard: risky code shapes in product Python (P25-01).

Every ``*.py`` file under ``src/`` and ``scripts/`` is parsed with ``ast`` (never imported or run)
and checked against the rules below. Names are resolved through the file's imports, so
``import subprocess as sp`` and ``from os import system`` are seen as what they are.

====================  ==========================================================================
rule                  what is reported
====================  ==========================================================================
``shell``             ``shell=`` other than a literal ``False`` on a ``subprocess`` spawn;
                      ``asyncio.create_subprocess_shell``; ``os.system``; ``os.popen``
``yaml``              ``yaml.load`` / ``load_all`` without a safe ``Loader``; ``unsafe_load``,
                      ``full_load``; ruamel ``YAML(typ="unsafe")``
``deserialize``       importing ``pickle``, ``marshal``, ``shelve``, ``dill``, ``jsonpickle`` …
``eval``              the builtins ``eval`` and ``exec``
``mktemp``            ``tempfile.mktemp``
``archive``           ``extractall`` / ``extract`` without ``filter=`` in a file that uses
                      ``tarfile`` or ``zipfile``; ``shutil.unpack_archive`` without ``filter=``
``decompress``        one-shot ``zlib`` / ``gzip`` / ``bz2`` / ``lzma`` ``decompress`` (no bound)
``tls``               ``verify=False``; ``ssl._create_unverified_context``; ``ssl.CERT_NONE``;
                      ``check_hostname = False``
``chmod``             a literal mode with world-write or set-id bits (``chmod``, ``os.open``,
                      ``mkdir``, ``makedirs``, ``touch``); a literal ``umask`` without ``0o002``
``xml``               importing ``xml.etree``, ``xml.dom``, ``xml.sax``, ``xml.parsers.expat``,
                      ``lxml`` (reviewed per file)
``inherit_env``       a spawn without ``env=`` (and without ``**kwargs``), ``os.exec*`` /
                      ``os.spawn*`` without an environment, ``pty.spawn`` (``src/`` only)
``secret_argv``       a spawn whose argv list names a secret-like value
``secret_url``        a URL query with a credential parameter written into a string
``secret_log``        a logging call whose arguments name a secret-like value (``src/`` only)
``timeout``           ``urlopen`` / ``requests.<verb>`` without ``timeout=``; ``timeout=None``
                      on an ``httpx`` client or call
====================  ==========================================================================

A secret-like value is an identifier whose last word (``_`` separated, case ignored) is one of
``SECRET_WORDS`` or that ends in ``api_key`` / ``private_key`` / ``secret_key``: ``bot_token`` and
``password`` are, ``token_count``, ``max_tokens`` and ``tokenizer`` are not.

``security_allow.json`` holds ``{"allow": {"<path>": {"<rule>": "<YYYY-MM-DD: reason>"}}}``. A
reason must start with its review date; an entry without one is a violation. An entry that no
longer matches anything is a warning so the list stays honest.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from tooling.checks.common import CHECKS_DIR, Context, Result, read_text, rel, walk_files

NAME = "security_static"
ALLOW_FILE = CHECKS_DIR / "security_allow.json"
SCAN_DIRS = ("src", "scripts")
SRC_ONLY = frozenset({"inherit_env", "secret_log"})
RULES = (
    "shell",
    "yaml",
    "deserialize",
    "eval",
    "mktemp",
    "archive",
    "decompress",
    "tls",
    "chmod",
    "xml",
    "inherit_env",
    "secret_argv",
    "secret_url",
    "secret_log",
    "timeout",
)
WHY = {
    "shell": "the command string goes through a shell",
    "yaml": "YAML that can build arbitrary objects",
    "deserialize": "deserialising can run code",
    "eval": "runs code built from a string",
    "mktemp": "the name can be taken before it is opened; use mkstemp / mkdtemp",
    "archive": "members can escape the folder; pass filter= or check members yourself",
    "decompress": "inflates without a size bound (decompression bomb); use a decompressobj "
    "with max_length",
    "tls": "turns certificate checks off",
    "chmod": "world-writable or set-id mode",
    "xml": "XML parser on input that may be hostile (entities); review the input",
    "inherit_env": "the child inherits the whole process environment; pass env= "
    "(indus.safety.child_env)",
    "secret_argv": "argv is readable by every local user",
    "secret_url": "URLs end up in logs, proxies and history",
    "secret_log": "the value ends up in log files",
    "timeout": "a slow peer can hold the caller forever",
}
SECRET_WORDS = frozenset(
    {
        "token",
        "secret",
        "password",
        "passwd",
        "passphrase",
        "apikey",
        "credential",
        "credentials",
        "ticket",
    }
)
_SECRET_PAIRS = (("api", "key"), ("private", "key"), ("secret", "key"))

_SPAWN = frozenset(
    {
        "subprocess.run",
        "subprocess.Popen",
        "subprocess.call",
        "subprocess.check_call",
        "subprocess.check_output",
    }
)
_ASYNC_SPAWN = frozenset({"asyncio.create_subprocess_exec", "asyncio.create_subprocess_shell"})
_SHELL_CALLS = frozenset({"asyncio.create_subprocess_shell", "os.system", "os.popen"})
_EXEC_NO_ENV = re.compile(r"^os\.(execv|execvp|execl|execlp|spawnv|spawnvp|spawnl|spawnlp)$")
_SAFE_LOADERS = frozenset({"SafeLoader", "CSafeLoader", "BaseLoader", "CBaseLoader"})
_YAML_UNSAFE = frozenset(
    {"yaml.unsafe_load", "yaml.unsafe_load_all", "yaml.full_load", "yaml.full_load_all"}
)
_DESERIALIZE = frozenset(
    {"pickle", "cPickle", "_pickle", "marshal", "shelve", "dill", "jsonpickle", "cloudpickle"}
)
_DECOMPRESS = frozenset({"zlib.decompress", "gzip.decompress", "bz2.decompress", "lzma.decompress"})
_XML_MODULES = ("xml.etree", "xml.dom", "xml.sax", "xml.parsers.expat", "lxml")
_MODE_CALLS = {
    "os.chmod": 1,
    "os.fchmod": 1,
    "os.lchmod": 1,
    "os.open": 2,
    "os.mkdir": 1,
    "os.makedirs": 1,
}
_MODE_METHODS = {"chmod": 0, "mkdir": 0, "touch": 0}
_REQUESTS = frozenset(
    f"requests.{v}" for v in ("get", "post", "put", "patch", "delete", "head", "options", "request")
)
_LOG_METHODS = frozenset(
    {"debug", "info", "warning", "warn", "error", "exception", "critical", "log", "fatal"}
)
_LOG_RECEIVERS = frozenset({"log", "logger", "logging", "_log", "_logger", "LOG", "LOGGER"})
_HTTP_MODULES = frozenset({"httpx", "requests", "aiohttp", "urllib3", "ssl", "websockets"})
_HTTP_METHODS = frozenset(
    {"get", "post", "put", "patch", "delete", "head", "options", "request", "stream", "send",
     "Client", "AsyncClient", "ClientSession", "connect"}
)  # fmt: skip
_URL_SECRET = re.compile(
    r"[?&](?:access_token|token|api_key|apikey|key|password|passwd|secret|client_secret)=",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Hit:
    line: int
    rule: str
    what: str


def is_secret_name(identifier: str) -> bool:
    words = [w for w in identifier.lower().split("_") if w]
    if not words:
        return False
    if words[-1] in SECRET_WORDS:
        return True
    return len(words) >= 2 and (words[-2], words[-1]) in _SECRET_PAIRS


def _imports(tree: ast.AST) -> dict[str, str]:
    """Local name → qualified name, from the file's import statements."""
    names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    names[alias.asname] = alias.name
                else:
                    top = alias.name.split(".", 1)[0]
                    names[top] = top
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                names[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return names


def _dotted(node: ast.expr) -> list[str] | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return parts[::-1]


def _qualified(node: ast.expr, imports: dict[str, str]) -> str | None:
    parts = _dotted(node)
    if not parts or parts[0] not in imports:
        return None
    return ".".join([imports[parts[0]], *parts[1:]])


def _kw(call: ast.Call, name: str) -> ast.keyword | None:
    return next((k for k in call.keywords if k.arg == name), None)


def _has_star_kwargs(call: ast.Call) -> bool:
    return any(k.arg is None for k in call.keywords)


def _is_const(node: ast.expr | None, value: object) -> bool:
    return isinstance(node, ast.Constant) and node.value is value


def _secret_names(node: ast.expr) -> list[str]:
    """Secret-like names whose value ``node`` shows as it is: a name, an attribute (its last
    part), a container or f-string of those. A call (``mask(token)``) shows its result, and
    ``ticket.generation`` shows the generation, so neither counts."""
    if isinstance(node, ast.Name):
        return [node.id] if is_secret_name(node.id) else []
    if isinstance(node, ast.Attribute):
        return [node.attr] if is_secret_name(node.attr) else []
    if isinstance(node, ast.JoinedStr):
        return [n for v in node.values if isinstance(v, ast.FormattedValue)
                for n in _secret_names(v.value)]  # fmt: skip
    if isinstance(node, ast.List | ast.Tuple | ast.Set):
        return [n for e in node.elts for n in _secret_names(e)]
    if isinstance(node, ast.BinOp):
        return _secret_names(node.left) + _secret_names(node.right)
    if isinstance(node, ast.Starred):
        return _secret_names(node.value)
    return []


def _docstring_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            ids.add(id(node.value))
    return ids


def _string_text(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else "{}"
            for v in node.values
        )
    return None


def _literal_mode(call: ast.Call, position: int) -> int | None:
    node = _kw(call, "mode")
    value = node.value if node is not None else None
    if value is None and len(call.args) > position:
        value = call.args[position]
    if isinstance(value, ast.Constant) and isinstance(value.value, int):
        return value.value
    return None


def _bad_mode(mode: int) -> bool:
    return bool(mode & 0o002 or mode & 0o6000)


class _Scanner:
    def __init__(self, tree: ast.Module, *, src: bool) -> None:
        self.tree = tree
        self.src = src
        self.imports = _imports(tree)
        self.hits: list[Hit] = []
        top = {q.split(".", 1)[0] for q in self.imports.values()}
        self.uses_archives = bool({"tarfile", "zipfile"} & top)
        self.docstrings = _docstring_ids(tree)

    def add(self, node: ast.AST, rule: str, what: str) -> None:
        if rule in SRC_ONLY and not self.src:
            return
        self.hits.append(Hit(getattr(node, "lineno", 0), rule, what))

    def run(self) -> list[Hit]:
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self._module(node, alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                self._module(node, node.module)
                for alias in node.names:
                    self._module(node, f"{node.module}.{alias.name}", from_name=True)
            elif isinstance(node, ast.Call):
                self._call(node)
            elif isinstance(node, ast.Attribute):
                if _qualified(node, self.imports) == "ssl.CERT_NONE":
                    self.add(node, "tls", "ssl.CERT_NONE")
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if (
                        isinstance(target, ast.Attribute)
                        and target.attr == "check_hostname"
                        and _is_const(node.value, False)
                    ):
                        self.add(node, "tls", "check_hostname = False")
            if isinstance(node, ast.Constant | ast.JoinedStr) and id(node) not in self.docstrings:
                text = _string_text(node)
                if text and _URL_SECRET.search(text):
                    self.add(node, "secret_url", "credential parameter in a URL string")
        unique = {(h.line, h.rule, h.what): h for h in self.hits}
        return sorted(unique.values(), key=lambda h: (h.line, h.rule, h.what))

    def _module(self, node: ast.AST, module: str, *, from_name: bool = False) -> None:
        top = module.split(".", 1)[0]
        if not from_name and top in _DESERIALIZE:
            self.add(node, "deserialize", f"import {module}")
        if not from_name and any(module == m or module.startswith(m + ".") for m in _XML_MODULES):
            self.add(node, "xml", f"import {module}")

    def _call(self, call: ast.Call) -> None:
        q = _qualified(call.func, self.imports)
        bare = call.func.id if isinstance(call.func, ast.Name) else None
        if bare in ("eval", "exec") and bare not in self.imports:
            self.add(call, "eval", f"{bare}()")
        if q in ("builtins.eval", "builtins.exec"):
            self.add(call, "eval", q)
        if q is not None:
            self._qualified_call(call, q)
        if isinstance(call.func, ast.Attribute):
            self._method_call(call, call.func.attr)
        verify = _kw(call, "verify")
        if verify is not None and _is_const(verify.value, False) and self._http_call(call, q):
            self.add(call, "tls", "verify=False")

    def _http_call(self, call: ast.Call, q: str | None) -> bool:
        if q is not None:
            return q.split(".", 1)[0] in _HTTP_MODULES
        return isinstance(call.func, ast.Attribute) and call.func.attr in _HTTP_METHODS

    def _qualified_call(self, call: ast.Call, q: str) -> None:
        spawn = q in _SPAWN or q in _ASYNC_SPAWN
        if q in _SPAWN:
            shell = _kw(call, "shell")
            if shell is not None and not _is_const(shell.value, False):
                self.add(call, "shell", f"{q}(shell=...)")
        if q in _SHELL_CALLS:
            self.add(call, "shell", q)
        if spawn:
            if _kw(call, "env") is None and not _has_star_kwargs(call):
                self.add(call, "inherit_env", f"{q} without env=")
            argv = call.args[0] if call.args else getattr(_kw(call, "args"), "value", None)
            if isinstance(argv, ast.List | ast.Tuple):
                names = _secret_names(argv)
                if names:
                    self.add(call, "secret_argv", f"{q} argv names {names[0]}")
            if q == "asyncio.create_subprocess_exec" and len(call.args) > 1:
                names = [n for a in call.args[1:] for n in _secret_names(a)]
                if names:
                    self.add(call, "secret_argv", f"{q} argv names {names[0]}")
        if _EXEC_NO_ENV.match(q) or q == "pty.spawn":
            self.add(call, "inherit_env", f"{q} inherits the environment")
        if q in ("yaml.load", "yaml.load_all"):
            loader = _kw(call, "Loader")
            value = loader.value if loader is not None else None
            if value is None and len(call.args) > 1:
                value = call.args[1]
            parts = _dotted(value) if value is not None else None
            if not parts or parts[-1] not in _SAFE_LOADERS:
                self.add(call, "yaml", f"{q} without a safe Loader")
        if q in _YAML_UNSAFE:
            self.add(call, "yaml", q)
        if q in ("ruamel.yaml.YAML",):
            typ = _kw(call, "typ")
            if (
                typ is not None
                and isinstance(typ.value, ast.Constant)
                and typ.value.value in ("unsafe",)
            ):
                self.add(call, "yaml", 'YAML(typ="unsafe")')
        if q in _DECOMPRESS:
            self.add(call, "decompress", q)
        if q == "tempfile.mktemp":
            self.add(call, "mktemp", q)
        if q == "shutil.unpack_archive" and _kw(call, "filter") is None:
            self.add(call, "archive", f"{q} without filter=")
        if q == "ssl._create_unverified_context":
            self.add(call, "tls", q)
        if q in _MODE_CALLS:
            mode = _literal_mode(call, _MODE_CALLS[q])
            if mode is not None and _bad_mode(mode):
                self.add(call, "chmod", f"{q} mode {mode:#o}")
        if q == "os.umask" and call.args:
            arg = call.args[0]
            mask = arg.value if isinstance(arg, ast.Constant) else None
            if isinstance(mask, int) and not mask & 0o002:
                self.add(call, "chmod", f"os.umask({mask:#o}) leaves world-write")
        if q == "urllib.request.urlopen" and _kw(call, "timeout") is None and len(call.args) < 3:
            self.add(call, "timeout", f"{q} without timeout=")
        if q in _REQUESTS and _kw(call, "timeout") is None:
            self.add(call, "timeout", f"{q} without timeout=")
        if q.startswith("httpx."):
            timeout = _kw(call, "timeout")
            if timeout is not None and _is_const(timeout.value, None):
                self.add(call, "timeout", f"{q}(timeout=None)")

    def _method_call(self, call: ast.Call, method: str) -> None:
        unpacks = method in ("extractall", "extract") and self.uses_archives
        if unpacks and _kw(call, "filter") is None:
            self.add(call, "archive", f".{method}() without filter=")
        if method in _MODE_METHODS and _qualified(call.func, self.imports) is None:
            mode = _literal_mode(call, _MODE_METHODS[method])
            if mode is not None and _bad_mode(mode):
                self.add(call, "chmod", f".{method}() mode {mode:#o}")
        if method in _LOG_METHODS:
            receiver = call.func.value if isinstance(call.func, ast.Attribute) else None
            parts = _dotted(receiver) if receiver is not None else None
            if parts and parts[-1] in _LOG_RECEIVERS:
                names = [n for a in call.args for n in _secret_names(a)]
                if names:
                    self.add(call, "secret_log", f"log call names {names[0]}")


def find_hits(source: str, *, src: bool = True) -> list[Hit]:
    """Rule hits in ``source`` (raises SyntaxError for unparsable text)."""
    return _Scanner(ast.parse(source), src=src).run()


def load_allow(path: Path = ALLOW_FILE) -> tuple[dict[str, dict[str, str]], list[str]]:
    """Return ({relative path: {rule: reason}}, problems)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {}, [f"{path.name}: cannot read: {exc}"]
    allow = data.get("allow") if isinstance(data, dict) else None
    if not isinstance(allow, dict):
        return {}, [f"{path.name}: needs an 'allow' object"]
    out: dict[str, dict[str, str]] = {}
    problems: list[str] = []
    for key, rules in allow.items():
        if not isinstance(rules, dict) or not rules:
            problems.append(f"{path.name}: {key} needs an object of rule: reason")
            continue
        kept: dict[str, str] = {}
        for rule, reason in rules.items():
            if rule not in RULES:
                problems.append(f"{path.name}: {key}: unknown rule {rule!r}")
            elif not isinstance(reason, str) or not re.match(r"^\d{4}-\d{2}-\d{2}: \S", reason):
                problems.append(f"{path.name}: {key}: {rule} needs a 'YYYY-MM-DD: reason'")
            else:
                kept[rule] = reason
        out[str(key)] = kept
    return out, problems


def _files(root: Path) -> Iterator[tuple[Path, bool]]:
    for folder in SCAN_DIRS:
        base = root / folder
        for path in walk_files(base):
            if path.suffix == ".py":
                yield path, folder == "src"


def run(ctx: Context, allow_file: Path = ALLOW_FILE) -> Result:
    result = Result(NAME)
    if not any((ctx.root / d).is_dir() for d in SCAN_DIRS):
        result.notes.append(f"none of {', '.join(SCAN_DIRS)} under the root; nothing to check")
        return result
    allow, problems = load_allow(allow_file)
    for problem in problems:
        result.fail(problem)
    used: set[tuple[str, str]] = set()
    counts: dict[str, int] = {}
    for path, src in _files(ctx.root):
        name = rel(path, ctx.root)
        text = read_text(path)
        if text is None:
            continue
        try:
            hits = find_hits(text, src=src)
        except SyntaxError as exc:
            result.fail(f"{name}: cannot parse ({exc.msg}, line {exc.lineno})")
            continue
        allowed = allow.get(name, {})
        for hit in hits:
            if hit.rule in allowed:
                used.add((name, hit.rule))
                continue
            counts[hit.rule] = counts.get(hit.rule, 0) + 1
            result.fail(f"{name}:{hit.line}: [{hit.rule}] {hit.what} — {WHY[hit.rule]}")
    for name, rules in sorted(allow.items()):
        for rule in sorted(rules):
            if (name, rule) in used:
                continue
            if (ctx.root / name).is_file():
                result.warnings.append(f"{ALLOW_FILE.name}: {name}: {rule} no longer matches")
            else:
                result.warnings.append(f"{ALLOW_FILE.name}: {name} does not exist")
    result.data["allowed"] = sorted(f"{n}:{r}" for n, r in used)
    result.data["violations_by_rule"] = counts
    return result


if __name__ == "__main__":
    from tooling.checks.__main__ import main

    raise SystemExit(main([NAME, *sys.argv[1:]]))
