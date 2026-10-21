# pyright: strict
"""The language-server catalogue (P24-15): which server handles a file and how it starts.

A ``ServerSpec`` names the file kinds it handles (suffixes, or whole base names such as
``Dockerfile``), the marker files that make a folder its project root, the binaries to look for
on the PATH and the arguments that put them in stdio mode. Nothing here installs anything: each
entry carries an install hint the CLI prints (``indus lsp install-hint``).

``project_code`` marks servers that load or run code the project ships (build scripts,
compiler plugins, the project's own node_modules). Those start only in a trusted workspace (the
session's own work tree or one listed in ``lsp.trusted_workspaces``); the others parse files and
are safe anywhere.

Config may add servers or change built-in ones under ``lsp.servers.<id>``: ``command`` (argv),
``extensions``, ``root_markers``, ``language_id``, ``env``, ``initialization_options`` and
``disabled``. A config entry with ``extensions`` and an id the catalogue does not know is a new
server; such servers count as running project code.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, cast

from indus.platform.locate import which

__all__ = [
    "CATALOGUE",
    "LANGUAGE_IDS",
    "ServerSpec",
    "find_git_root",
    "language_id",
    "match_key",
    "merge_config",
    "project_root",
    "resolve_command",
    "server_for",
]

log = logging.getLogger(__name__)

MAX_WALK = 64


@dataclass(frozen=True, slots=True)
class ServerSpec:
    """One language server (see the module notes)."""

    server_id: str
    handles: tuple[str, ...]
    binaries: tuple[str, ...]
    args: tuple[str, ...] = ()
    markers: tuple[str, ...] = ()
    install_hint: str = ""
    label: str = ""
    project_code: bool = True
    language: str = ""
    env: Mapping[str, str] = field(default_factory=lambda: dict[str, str]())
    init_options: Mapping[str, Any] = field(default_factory=lambda: dict[str, Any]())
    command: tuple[str, ...] = ()  # a configured argv replaces binaries + args
    disabled: bool = False

    def matches(self, path: str) -> bool:
        return match_key(path) in self.handles


# Language ids sent with didOpen (some servers refuse a wrong one): "<id> <suffix> <suffix>...".
_LANGUAGE_TABLE = """
python .py .pyi
typescript .ts .mts .cts
typescriptreact .tsx
javascript .js .mjs .cjs
javascriptreact .jsx
vue .vue
svelte .svelte
astro .astro
go .go
rust .rs
c .c .h
cpp .cc .cpp .cxx .hh .hpp .hxx
shellscript .sh .bash .zsh .ksh
yaml .yaml .yml
lua .lua
php .php
dockerfile .dockerfile dockerfile
terraform .tf .tfvars
zig .zig .zon
gleam .gleam
elixir .ex .exs
kotlin .kt .kts
java .java
ocaml .ml .mli
haskell .hs .lhs
clojure .clj .cljc .cljs .edn
nix .nix
dart .dart
ruby .rb .rake .gemspec
"""
LANGUAGE_IDS: dict[str, str] = {
    key: row.split()[0] for row in _LANGUAGE_TABLE.strip().splitlines() for key in row.split()[1:]
}

_JS_ROOTS = (
    "pnpm-lock.yaml",
    "bun.lock",
    "bun.lockb",
    "yarn.lock",
    "package-lock.json",
    "package.json",
    "tsconfig.json",
)
_JS = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")

CATALOGUE: tuple[ServerSpec, ...] = (
    ServerSpec(
        "pyright",
        (".py", ".pyi"),
        ("pyright-langserver", "basedpyright-langserver"),
        ("--stdio",),
        ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "pyrightconfig.json"),
        "npm install -g pyright   (or: pip install basedpyright)",
        "Python (pyright / basedpyright)",
        project_code=False,
    ),
    ServerSpec(
        "typescript",
        _JS,
        ("typescript-language-server",),
        ("--stdio",),
        _JS_ROOTS,
        "npm install -g typescript typescript-language-server",
        "TypeScript and JavaScript (typescript-language-server)",
    ),
    ServerSpec(
        "gopls",
        (".go",),
        ("gopls",),
        (),
        ("go.work", "go.mod"),
        "go install golang.org/x/tools/gopls@latest",
        "Go (gopls)",
    ),
    ServerSpec(
        "rust-analyzer",
        (".rs",),
        ("rust-analyzer",),
        (),
        ("Cargo.toml",),
        "rustup component add rust-analyzer",
        "Rust (rust-analyzer)",
    ),
    ServerSpec(
        "clangd",
        (".c", ".h", ".cc", ".cpp", ".cxx", ".hh", ".hpp", ".hxx"),
        ("clangd",),
        ("--background-index",),
        ("compile_commands.json", "compile_flags.txt", ".clangd"),
        "install clangd from your package manager (apt install clangd, brew install llvm)",
        "C and C++ (clangd)",
        project_code=False,
    ),
    ServerSpec(
        "bash-language-server",
        (".sh", ".bash", ".zsh", ".ksh"),
        ("bash-language-server",),
        ("start",),
        (),
        "npm install -g bash-language-server   (diagnostics need shellcheck on the PATH)",
        "Shell scripts (bash-language-server)",
        project_code=False,
    ),
    ServerSpec(
        "yaml-language-server",
        (".yaml", ".yml"),
        ("yaml-language-server",),
        ("--stdio",),
        (),
        "npm install -g yaml-language-server",
        "YAML (yaml-language-server)",
        project_code=False,
    ),
    ServerSpec(
        "dockerfile-language-server",
        (".dockerfile", "dockerfile"),
        ("docker-langserver",),
        ("--stdio",),
        (),
        "npm install -g dockerfile-language-server-nodejs",
        "Dockerfiles (dockerfile-language-server)",
        project_code=False,
    ),
    ServerSpec(
        "intelephense",
        (".php",),
        ("intelephense",),
        ("--stdio",),
        ("composer.json",),
        "npm install -g intelephense",
        "PHP (intelephense)",
        project_code=False,
        init_options={"telemetry": {"enabled": False}},
    ),
    ServerSpec(
        "lua-language-server",
        (".lua",),
        ("lua-language-server",),
        (),
        (".luarc.json", ".luarc.jsonc", "stylua.toml"),
        "install lua-language-server from its releases or your package manager",
        "Lua (lua-language-server)",
    ),
    ServerSpec(
        "svelte-language-server",
        (".svelte",),
        ("svelteserver",),
        ("--stdio",),
        _JS_ROOTS,
        "npm install -g svelte-language-server",
        "Svelte (svelte-language-server)",
    ),
    ServerSpec(
        "vue-language-server",
        (".vue",),
        ("vue-language-server",),
        ("--stdio",),
        _JS_ROOTS,
        "npm install -g @vue/language-server",
        "Vue (vue-language-server)",
    ),
    ServerSpec(
        "terraform-ls",
        (".tf", ".tfvars"),
        ("terraform-ls",),
        ("serve",),
        (".terraform.lock.hcl",),
        "install terraform-ls from HashiCorp's releases",
        "Terraform (terraform-ls)",
    ),
    ServerSpec(
        "zls",
        (".zig", ".zon"),
        ("zls",),
        (),
        ("build.zig",),
        "install zls from its releases",
        "Zig (zls)",
    ),
    ServerSpec(
        "gleam",
        (".gleam",),
        ("gleam",),
        ("lsp",),
        ("gleam.toml",),
        "install gleam (the language server is built in)",
        "Gleam (gleam lsp)",
    ),
    ServerSpec(
        "elixir-ls",
        (".ex", ".exs"),
        ("elixir-ls", "language_server.sh"),
        (),
        ("mix.exs",),
        "install elixir-ls from its releases",
        "Elixir (elixir-ls)",
    ),
    ServerSpec(
        "kotlin-language-server",
        (".kt", ".kts"),
        ("kotlin-language-server",),
        (),
        ("settings.gradle", "settings.gradle.kts", "build.gradle", "build.gradle.kts", "pom.xml"),
        "install kotlin-language-server from its releases",
        "Kotlin (kotlin-language-server)",
    ),
    ServerSpec(
        "jdtls",
        (".java",),
        ("jdtls",),
        (),
        ("pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle"),
        "install Eclipse JDT LS (the jdtls launcher must be on the PATH)",
        "Java (Eclipse JDT LS)",
    ),
    ServerSpec(
        "ocamllsp",
        (".ml", ".mli"),
        ("ocamllsp",),
        (),
        ("dune-project", "dune-workspace", "opam"),
        "opam install ocaml-lsp-server",
        "OCaml (ocaml-lsp)",
    ),
    ServerSpec(
        "haskell-language-server",
        (".hs", ".lhs"),
        ("haskell-language-server-wrapper", "haskell-language-server"),
        ("--lsp",),
        ("stack.yaml", "cabal.project", "hie.yaml"),
        "ghcup install hls",
        "Haskell (haskell-language-server)",
    ),
    ServerSpec(
        "clojure-lsp",
        (".clj", ".cljc", ".cljs", ".edn"),
        ("clojure-lsp",),
        (),
        ("deps.edn", "project.clj", "shadow-cljs.edn", "bb.edn"),
        "install clojure-lsp from its releases",
        "Clojure (clojure-lsp)",
    ),
    ServerSpec(
        "nixd",
        (".nix",),
        ("nixd",),
        (),
        ("flake.nix",),
        "nix profile install nixpkgs#nixd",
        "Nix (nixd)",
    ),
    ServerSpec(
        "dart",
        (".dart",),
        ("dart",),
        ("language-server", "--lsp"),
        ("pubspec.yaml",),
        "install the Dart SDK (the language server is built in)",
        "Dart (dart language-server)",
    ),
)
"""The built-in servers, first match wins (config-declared servers come before these)."""


def match_key(path: str) -> str:
    """What a server matches on: the lower-case suffix, or the lower-case base name when the
    file has none (``Dockerfile`` → ``dockerfile``)."""
    base = os.path.basename(path.replace("\\", "/"))
    suffix = os.path.splitext(base)[1].lower()
    return suffix or base.lower()


def language_id(path: str, spec: ServerSpec | None = None) -> str:
    if spec is not None and spec.language:
        return spec.language
    return LANGUAGE_IDS.get(match_key(path), "plaintext")


def server_for(path: str, servers: Sequence[ServerSpec]) -> ServerSpec | None:
    return next((s for s in servers if s.matches(path)), None)


# -- roots ---------------------------------------------------------------------------------------


def _start_dir(path: str) -> Path | None:
    try:
        start = Path(os.path.abspath(os.path.expanduser(path)))
        return start.parent if not start.is_dir() else start
    except (OSError, ValueError, RuntimeError):
        return None


def _upwards(start: Path) -> Iterable[Path]:
    current = start
    for _ in range(MAX_WALK):
        yield current
        if current.parent == current:
            return
        current = current.parent


def find_git_root(path: str) -> str | None:
    """The nearest folder at or above ``path`` holding ``.git`` (a dir, or a worktree's file)."""
    start = _start_dir(path)
    if start is None:
        return None
    for folder in _upwards(start):
        try:
            if (folder / ".git").exists():
                return str(folder)
        except OSError:
            return None
    return None


def project_root(path: str, workspace: str, markers: Sequence[str]) -> str:
    """The nearest folder between ``path`` and ``workspace`` holding one of ``markers``; a
    Python package folder (one with ``__init__.py``) is never a root. ``workspace`` when none
    is found or the server has no markers."""
    start = _start_dir(path)
    if start is None or not markers:
        return workspace
    top = Path(workspace)
    for folder in _upwards(start):
        try:
            package = (folder / "__init__.py").exists()
            if not package and any((folder / m).exists() for m in markers):
                return str(folder)
        except OSError:
            break
        if folder == top:
            break
    return workspace


# -- binaries ------------------------------------------------------------------------------------


def resolve_command(spec: ServerSpec, search_path: str | None) -> list[str] | None:
    """The argv that starts ``spec``: the configured command (its first word looked up on the
    PATH unless it is a path), else the first catalogue binary found. None when nothing is
    installed."""
    if spec.command:
        first = os.path.expanduser(spec.command[0])
        found = first if os.sep in first and os.access(first, os.X_OK) else None
        found = found or which(first, path=search_path)
        return None if found is None else [found, *spec.command[1:]]
    for name in spec.binaries:
        found = which(name, path=search_path)
        if found is not None:
            return [found, *spec.args]
    return None


# -- config --------------------------------------------------------------------------------------


def _strings(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, list):
        return None
    items = cast(list[object], value)
    if not items or not all(isinstance(v, str) and v for v in items):
        return None
    return tuple(cast(list[str], items))


def _keys(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(v.lower() for v in values)


def _overlay(spec: ServerSpec, raw: Mapping[str, Any]) -> ServerSpec:
    changes: dict[str, Any] = {}
    command = _strings(raw.get("command"))
    if command is not None:
        changes["command"] = command
    extensions = _strings(raw.get("extensions"))
    if extensions is not None:
        changes["handles"] = _keys(extensions)
    markers = _strings(raw.get("root_markers"))
    if markers is not None:
        changes["markers"] = markers
    env = raw.get("env")
    if isinstance(env, Mapping):
        items = cast(Mapping[object, object], env).items()
        changes["env"] = {str(k): str(v) for k, v in items}
    init = raw.get("initialization_options")
    if isinstance(init, Mapping):
        changes["init_options"] = dict(cast(Mapping[str, Any], init))
    lang = raw.get("language_id")
    if isinstance(lang, str) and lang:
        changes["language"] = lang
    if "disabled" in raw:
        changes["disabled"] = bool(raw.get("disabled"))
    return replace(spec, **changes)


def merge_config(
    servers_cfg: object, base: Sequence[ServerSpec] = CATALOGUE
) -> tuple[ServerSpec, ...]:
    """``base`` changed by ``lsp.servers``: entries for known ids adjust them; an unknown id with
    ``command`` and ``extensions`` is a new server placed ahead of the catalogue; anything else
    is logged and ignored (one typo never disables the rest)."""
    if not isinstance(servers_cfg, Mapping):
        return tuple(base)
    known = {s.server_id: s for s in base}
    adjusted: dict[str, ServerSpec] = {}
    added: list[ServerSpec] = []
    for key, raw in cast(Mapping[object, object], servers_cfg).items():
        server_id = str(key)
        if not isinstance(raw, Mapping):
            log.warning("lsp.servers.%s must be a mapping; ignored", server_id)
            continue
        entry = cast(Mapping[str, Any], raw)
        if server_id in known:
            adjusted[server_id] = _overlay(known[server_id], entry)
            continue
        command = _strings(entry.get("command"))
        extensions = _strings(entry.get("extensions"))
        if command is None or extensions is None:
            log.warning(
                "lsp.servers.%s: a new server needs `command: [binary, args...]` and "
                "`extensions: [.ext, ...]`; ignored",
                server_id,
            )
            continue
        added.append(
            _overlay(
                ServerSpec(
                    server_id,
                    (),
                    (),
                    label=f"{server_id} (configured)",
                    install_hint="configured in lsp.servers; install it yourself",
                ),
                entry,
            )
        )
    return (*added, *(adjusted.get(s.server_id, s) for s in base))
