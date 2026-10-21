"""The Nix flake, packages and modules (P23-13, P23-14), checked without nixpkgs.

`nix flake check` (the Nix CI lane) builds the packages and evaluates the modules. Here we check
what needs no network: the files' structure and names, the variant data against `pyproject.toml`,
the generated pins, the MCP fields against our schema, and the Nix-install kind. When
`nix-instantiate` is on PATH every file is parsed and the builtins-only renderer is evaluated
(that part skips cleanly without Nix).
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from indus.cli.version import install_method
from indus.mcp.config import McpServer

ROOT = Path(__file__).resolve().parents[3]
NIX = ROOT / "nix"
FLAKE = ROOT / "flake.nix"
NIX_FILES = sorted([FLAKE, *NIX.glob("*.nix")])
PACKAGES = {"default", "minimal", "messaging", "tui", "web"}
VARIANTS = {"default", "minimal", "messaging"}
INPUTS = {"nixpkgs", "flake-parts", "pyproject-nix", "uv2nix", "pyproject-build-systems",
          "home-manager"}  # fmt: skip
SYSTEMS = ["x86_64-linux", "aarch64-linux", "aarch64-darwin"]
SHARED_OPTIONS = {
    "enable",
    "package",
    "workingDirectory",
    "configFile",
    "settings",
    "environmentFiles",
    "environment",
    "extraPackages",
    "extraPythonPackages",
    "mcpServers",
    "extraArgs",
    "restart",
    "restartSec",
}
MODULE_CASES = {
    "module-native-default",
    "module-container-mode",
    "module-container-and-native-refused",
    "module-neither-mode-refused",
    "module-container-needs-image",
    "module-config-file-and-settings-refused",
    "module-mcp-command-and-url-refused",
    "module-settings-rendered",
    "module-mcp-rendered",
    "module-home-manager",
}

NIX_INSTANTIATE = shutil.which("nix-instantiate")
needs_nix = pytest.mark.skipif(NIX_INSTANTIATE is None, reason="nix-instantiate is not installed")


# -- helpers ----------------------------------------------------------------------------------


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _strings(block: str) -> list[str]:
    return re.findall(r'"([^"]*)"', block)


def _block(text: str, opener: str) -> str:
    """The text between ``opener`` (ending in ``{`` or ``[``) and its matching closer."""
    start = text.index(opener) + len(opener)
    pairs = {"{": "}", "[": "]"}
    open_char = opener.rstrip()[-1]
    depth = 1
    for index in range(start, len(text)):
        char = text[index]
        if char == open_char:
            depth += 1
        elif char == pairs[open_char]:
            depth -= 1
            if depth == 0:
                return text[start:index]
    raise AssertionError(f"no closer for {opener!r}")


def _variants_from_text() -> dict[str, list[str]]:
    """Read nix/variants.nix without Nix: ``name = [ ... ];`` and ``name = other ++ [ ... ];``."""
    text = re.sub(r"#[^\n]*", "", _text(NIX / "variants.nix"))
    lists: dict[str, list[str]] = {}
    for match in re.finditer(r"([\w-]+)\s*=\s*(?:([\w-]+)\s*\+\+\s*)?\[([^\]]*)\]\s*;", text):
        name, base, items = match.group(1), match.group(2), match.group(3)
        lists[name] = [*(lists[base] if base else []), *_strings(items)]
    for name in re.findall(r"inherit\s+([\w\s-]+);", text):
        for inherited in name.split():
            assert inherited in lists, f"inherit of an unknown list {inherited!r}"
    return lists


def _pyproject() -> dict[str, Any]:
    return tomllib.loads(_text(ROOT / "pyproject.toml"))


def _package_names() -> set[str]:
    text = _text(NIX / "packages.nix")
    section = text[text.index("# packages:begin") : text.index("# packages:end")]
    body = _block(section, "packages = {")
    return set(re.findall(r"^ {8}([\w-]+)\s*=", body, re.MULTILINE))


def _nix_env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "NIX_CONFIG": "experimental-features = nix-command",
    }


def _nix_eval(expr: str, tmp_path: Path) -> Any:
    assert NIX_INSTANTIATE is not None
    done = subprocess.run(
        [NIX_INSTANTIATE, "--eval", "--strict", "--json", "--expr", expr],
        cwd=ROOT,
        env=_nix_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def _load_pins_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("nix_pins", ROOT / "scripts" / "nix" / "pins.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# -- flake.nix ----------------------------------------------------------------------------------


def test_flake_inputs_follow_nixpkgs() -> None:
    body = _block(_text(FLAKE), "inputs = {")
    names = set(re.findall(r"^ {4}([\w-]+)(?:\.url)?\s*=", body, re.MULTILINE))
    assert names == INPUTS
    for name in INPUTS - {"nixpkgs"}:
        entry = _block(body, f"{name} = {{")
        assert re.search(r'inputs\.nixpkgs(?:-lib)?\.follows = "nixpkgs";', entry), name
        assert re.search(r'url = "github:[\w-]+/[\w.-]+";', entry), name
    assert 'nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";' in body


def test_flake_systems() -> None:
    assert _strings(_block(_text(FLAKE), "systems = [")) == SYSTEMS


def test_flake_imports_and_modules_exist() -> None:
    text = _text(FLAKE)
    referenced = set(re.findall(r"\./(nix/[\w-]+\.nix)", text))
    assert referenced == {
        "nix/packages.nix",
        "nix/checks.nix",
        "nix/module-checks.nix",
        "nix/nixos-module.nix",
        "nix/home-manager-module.nix",
    }
    for rel in referenced:
        assert (ROOT / rel).is_file(), rel
    for output in ("overlays.default", "nixosModules.default", "homeManagerModules.default"):
        assert f"{output} =" in text


# -- packages and variants ----------------------------------------------------------------------


def test_packages_are_the_planned_set() -> None:
    assert _package_names() == PACKAGES
    text = _text(NIX / "packages.nix")
    used = set(re.findall(r'name = "([\w-]+)";\s*withTui', text))
    assert used == VARIANTS


def test_variant_nesting() -> None:
    variants = _variants_from_text()
    assert set(variants) == VARIANTS
    assert variants["minimal"] == []
    assert set(variants["minimal"]) <= set(variants["messaging"]) <= set(variants["default"])
    assert {"discord", "slack"} <= set(variants["messaging"])
    for name, extras in variants.items():
        assert len(extras) == len(set(extras)), f"{name} repeats an extra"


def test_variant_extras_are_declared() -> None:
    declared = set(_pyproject()["project"]["optional-dependencies"])
    for name, extras in _variants_from_text().items():
        unknown = set(extras) - declared
        assert not unknown, f"variant {name} names undeclared extras {sorted(unknown)}"


@needs_nix
def test_variants_evaluate_to_the_same_data(tmp_path: Path) -> None:
    assert _nix_eval("import ./nix/variants.nix", tmp_path) == _variants_from_text()


def test_nix_build_sets_the_guard_variable() -> None:
    text = _text(NIX / "packages.nix")
    assert text.count('INDUS_NIX_BUILD = "1";') == 1
    override = _block(text, "indus-agent = prev.indus-agent.overrideAttrs (old: {")
    assert 'INDUS_NIX_BUILD = "1";' in override
    assert "src/indus/web/static" in override


def test_packages_use_the_pins() -> None:
    text = _text(NIX / "packages.nix")
    for key in ("python", "nodejs", "pnpm", "pnpmDepsHash"):
        assert f"pins.{key}" in text, key
    assert "lib.fakeHash" in text
    assert "loadWorkspace { workspaceRoot = ../.; }" in text
    assert "INDUS_TUI_DIR" in text


def test_checks_cover_every_package() -> None:
    text = _text(NIX / "checks.nix")
    assert 'lib.nameValuePair "build-${name}" drv) self\'.packages' in text
    for name in ("build-devshell", "variant-extras", "version"):
        assert re.search(rf"^ {{10}}{name} =", text, re.MULTILINE), name
    assert "Install method: nix" in text


def test_docs_name_only_existing_packages() -> None:
    docs = [ROOT / "README.md", *sorted((ROOT / "docs" / "user").rglob("*.md"))]
    docs += sorted((ROOT / "docs" / "notes" / "P23").glob("*.md"))
    known = PACKAGES | {"devShells", "checks", "nixosModules", "homeManagerModules"}
    named: set[str] = set()
    for doc in docs:
        named |= set(re.findall(r"\.#([A-Za-z][\w-]*[A-Za-z0-9])", _text(doc)))
    assert named, "no document names a flake output yet"
    assert named <= known, f"documents name missing outputs: {sorted(named - known)}"


# -- pins ---------------------------------------------------------------------------------------


def test_pins_are_fresh() -> None:
    pins = _load_pins_script()
    assert pins.main(["--check"]) == 0
    data = json.loads(_text(NIX / "pins.json"))
    assert data["python"] == "python" + _text(ROOT / ".python-version").strip().replace(".", "")
    engines = json.loads(_text(ROOT / "package.json"))["engines"]["node"]
    assert data["nodejs"] == "nodejs_" + re.findall(r"\d+", engines)[0]
    assert data["pnpmDepsHash"] is None or data["pnpmDepsHash"].startswith("sha256-")


def _copy_repo_bits(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    for rel in (
        ".python-version",
        "package.json",
        "src/indus/foundation/config/schema.py",
        "src/indus/foundation/versions.py",
        "nix/pins.json",
    ):
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / rel, target)
    return root


def test_pins_check_fails_when_the_repo_moves(tmp_path: Path) -> None:
    pins = _load_pins_script()
    root = _copy_repo_bits(tmp_path)
    assert pins.main(["--check"], root=root) == 0
    (root / ".python-version").write_text("3.14\n", encoding="utf-8")
    assert pins.main(["--check"], root=root) == 1
    assert pins.main([], root=root) == 0
    assert json.loads((root / "nix/pins.json").read_text(encoding="utf-8"))["python"] == "python314"
    assert pins.main(["--check"], root=root) == 0


def test_pins_check_fails_on_a_hand_edit(tmp_path: Path) -> None:
    pins = _load_pins_script()
    root = _copy_repo_bits(tmp_path)
    path = root / "nix/pins.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["nodejs"] = "nodejs_18"
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    assert pins.main(["--check"], root=root) == 1


def test_pnpm_hash_is_kept_and_validated(tmp_path: Path) -> None:
    pins = _load_pins_script()
    root = _copy_repo_bits(tmp_path)
    good = "sha256-" + "A" * 43 + "="
    assert pins.main(["--pnpm-hash", "md5-nope"], root=root) == 2
    assert pins.main(["--pnpm-hash", good], root=root) == 0
    assert pins.main(["--check"], root=root) == 0
    data = json.loads((root / "nix/pins.json").read_text(encoding="utf-8"))
    assert data["pnpmDepsHash"] == good
    data["pnpmDepsHash"] = "not a hash"
    (root / "nix/pins.json").write_text(json.dumps(data), encoding="utf-8")
    assert pins.main(["--check"], root=root) == 2


# -- modules ------------------------------------------------------------------------------------


def _common_option_names() -> set[str]:
    body = _block(_text(NIX / "module-common.nix"), "\n  options = {")
    return set(re.findall(r"^ {4}([\w]+) = (?:mkOption|lib\.mkEnableOption)", body, re.M))


def test_common_options_declared() -> None:
    assert _common_option_names() == SHARED_OPTIONS | {"renderedConfig"}
    for module in ("nixos-module.nix", "home-manager-module.nix"):
        text = _text(NIX / module)
        assert "common.options // {" in text, module
        own = set(re.findall(r"^ {4}([\w]+)(?:\.enable)? = (?:mkOption|lib\.mkEnable)", text, re.M))
        assert not own & SHARED_OPTIONS, f"{module} redeclares {sorted(own & SHARED_OPTIONS)}"
        assert "assertions = common.assertionsFor" in text, module


def test_module_specific_options() -> None:
    nixos = _text(NIX / "nixos-module.nix")
    assert "options.services.indus-agent = common.options" in nixos
    for name in ("user", "group", "createUser", "stateDir", "addToSystemPackages", "native.enable"):
        assert re.search(rf"^ {{4}}{re.escape(name)} = mkOption", nixos, re.M), name
    container = _block(nixos, "    container = {")
    for name in ("enable", "backend", "image", "extraOptions", "extraVolumes"):
        assert re.search(rf"^ {{6}}{name} = ", container, re.M), name
    assert '"docker"' in container and '"podman"' in container
    assert "linger = lib.mkDefault nativeMode;" in nixos
    home = _text(NIX / "home-manager-module.nix")
    assert "options.programs.indus-agent = common.options" in home
    for name in ("home", "installPackage", "gateway.enable"):
        assert re.search(rf"^ {{4}}{re.escape(name)} = ", home, re.M), name
    assert "systemd.user.services.indus-agent" in home
    assert "launchd.agents.indus-agent" in home


def test_secrets_never_go_through_the_store() -> None:
    common = _text(NIX / "module-common.nix")
    env_files = _block(common, "    environmentFiles = mkOption {")
    assert "type = types.listOf types.str;" in env_files
    nixos = _text(NIX / "nixos-module.nix")
    assert "EnvironmentFile = cfg.environmentFiles;" in nixos
    assert "environmentFiles = cfg.environmentFiles;" in nixos


def _render_fields() -> list[str]:
    return _strings(_block(_text(NIX / "render.nix"), "mcpTypedFields = ["))


def test_mcp_fields_exist_in_schema() -> None:
    fields = _render_fields()
    assert fields, "render.nix declares no typed MCP fields"
    assert set(fields) <= set(McpServer.model_fields)
    body = _block(_text(NIX / "module-common.nix"), "mcpServerType = types.submodule {")
    declared = set(re.findall(r"^ {6}([\w]+) = mkOption", body, re.M))
    assert declared == set(fields) | {"extraConfig"}


def test_module_checks_name_every_case() -> None:
    text = _text(NIX / "module-checks.nix")
    assert set(re.findall(r"^ {8}(module-[\w-]+) =", text, re.M)) == MODULE_CASES


# -- evaluation with nix-instantiate (skipped without Nix) --------------------------------------


@needs_nix
@pytest.mark.parametrize("path", NIX_FILES, ids=lambda p: p.name)
def test_every_nix_file_parses(path: Path, tmp_path: Path) -> None:
    assert NIX_INSTANTIATE is not None
    done = subprocess.run(
        [NIX_INSTANTIATE, "--parse", str(path)],
        env=_nix_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr


RENDER = "(import ./nix/render.nix)"
SERVER_NULLS = (
    "args = [ ]; env = { }; cwd = null; transport = null; headers = { }; auth = null; "
    "enabled = true; extraConfig = { };"
)


@needs_nix
def test_render_config(tmp_path: Path) -> None:
    expr = (
        f'{RENDER}.renderConfig {{ configVersion = 1; workingDirectory = "/srv/indus"; '
        'settings = { model.default = "p/m"; terminal.backend = "local"; '
        'mcp_servers.old = { command = "x"; }; }; }'
    )
    assert _nix_eval(expr, tmp_path) == {
        "_config_version": 1,
        "model": {"default": "p/m"},
        "terminal": {"backend": "local", "cwd": "/srv/indus"},
        "mcp_servers": {"old": {"command": "x"}},
    }
    # A stamp in the settings wins; no working directory leaves terminal alone.
    expr = f"{RENDER}.renderConfig {{ configVersion = 1; settings._config_version = 7; }}"
    assert _nix_eval(expr, tmp_path) == {"_config_version": 7}


@needs_nix
def test_render_mcp_server(tmp_path: Path) -> None:
    expr = (
        f"{RENDER}.renderConfig {{ configVersion = 1; mcpServers = {{ "
        f'files = {{ command = "mcp-files"; url = null; {SERVER_NULLS} }} // '
        '{ args = [ "--root" "/srv" ]; }; '
        'remote = { command = null; url = "https://mcp.example.invalid/mcp"; '
        "args = [ ]; env = { }; cwd = null; headers = { }; "
        'transport = "http"; auth = "oauth"; enabled = false; '
        'extraConfig = { timeout = 30; url = "ignored"; }; }; }; }'
    )
    assert _nix_eval(expr, tmp_path)["mcp_servers"] == {
        "files": {"command": "mcp-files", "args": ["--root", "/srv"]},
        "remote": {
            "url": "https://mcp.example.invalid/mcp",
            "transport": "http",
            "auth": "oauth",
            "enabled": False,
            "timeout": 30,
        },
    }


@needs_nix
@pytest.mark.parametrize(
    ("args", "needles"),
    [
        ("native = true; container = false;", []),
        ('native = true; container = true; containerImage = "i";', ["both enabled"]),
        ("native = false; container = false;", ["nothing would run"]),
        ("native = false; container = true;", ["needs container.image"]),
        ("enable = false; native = true; container = true;", []),
        ('configFile = "/etc/c.yaml"; settings.a = 1;', ["do not also set settings"]),
        ('mcpServers.s = { command = "c"; url = "u"; };', ["mcpServers.s", "not both"]),
        ("mcpServers.s = { };", ["set command (stdio) or url"]),
        ('mcpServers.s = { command = "c"; transport = "sse"; };', ["url servers only"]),
    ],
)
def test_render_problems_for_modes(args: str, needles: list[str], tmp_path: Path) -> None:
    expr = f'{RENDER}.problems {{ optionPath = "services.indus-agent"; {args} }}'
    problems = _nix_eval(expr, tmp_path)
    if not needles:
        assert problems == []
        return
    joined = "\n".join(problems)
    assert all(problem.startswith("services.indus-agent") for problem in problems)
    for needle in needles:
        assert needle in joined


# -- the Nix install kind -----------------------------------------------------------------------


def test_a_store_install_is_the_nix_kind() -> None:
    package = Path("/nix/store/0000000000000000000000000000000-indus-agent-0.0.0") / (
        "lib/python3.13/site-packages/indus"
    )
    assert install_method(package, exists=lambda _path: False) == "nix"
    # A container marker does not turn a store install into a Docker one.
    assert install_method(package, exists=lambda _path: True) == "nix"
