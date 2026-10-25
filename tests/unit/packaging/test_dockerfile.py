"""The Dockerfiles read statically (P23-08, P23-09): stages, user, volume, entry point, pinned
bases, and nothing fetched unverified. The built images are tested in ``tests/docker``."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.unit.packaging.docker_files import (
    DOCKER_FILES,
    DOCKERFILE,
    ROOT,
    SANDBOX_DOCKERFILE,
    banned_hits,
    instructions,
    load_pins,
    stages,
)

PRODUCT = DOCKERFILE.read_text(encoding="utf-8")
SANDBOX = SANDBOX_DOCKERFILE.read_text(encoding="utf-8")
pins = load_pins()


def last(stage: list[tuple[str, str]], word: str) -> str:
    found = [rest for w, rest in stage if w == word]
    assert found, f"no {word}"
    return found[-1]


def env_of(stage: list[tuple[str, str]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for word, rest in stage:
        if word == "ENV":
            for m in re.finditer(r"(\w+)=(\S+)", rest):
                out[m.group(1)] = m.group(2)
    return out


# -- product image --------------------------------------------------------------------------------


def test_product_stages_in_order() -> None:
    assert list(stages(PRODUCT)) == ["fetch", "web", "build", "product"]


def test_every_from_uses_the_pinned_base_arg_or_an_earlier_stage() -> None:
    for text in (PRODUCT, SANDBOX):
        seen: list[str] = []
        for word, rest in instructions(text):
            if word != "FROM":
                continue
            source = rest.split()[0]
            assert source == "${BASE_IMAGE}" or source in seen, rest
            parts = rest.split()
            if len(parts) >= 3:
                seen.append(parts[-1])


def test_base_arg_default_is_the_pinned_reference() -> None:
    expected = pins.base_reference(json.loads((ROOT / "docker/pins.json").read_text("utf-8")))
    assert pins.dockerfile_base(PRODUCT) == expected
    assert pins.dockerfile_base(SANDBOX) == expected
    assert ":latest" not in PRODUCT and ":latest" not in SANDBOX


def test_final_stage_user_volume_entrypoint_and_env() -> None:
    final = stages(PRODUCT)["product"]
    run = " ".join(rest for word, rest in final if word == "RUN")
    assert "--uid 10000" in run and "--gid 10000" in run and "--home-dir /opt/data" in run
    assert last(final, "WORKDIR") == "/opt/indus"
    assert last(final, "VOLUME") == '["/opt/data"]'
    assert last(final, "ENTRYPOINT") == '["/opt/indus/docker/entrypoint.sh"]'
    assert last(final, "CMD") == "[]"
    env = env_of(final)
    assert env["INDUS_HOME"] == "/opt/data"
    assert env["PYTHONUNBUFFERED"] == "1"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert env["PATH"].startswith("/opt/indus/.venv/bin:")
    assert env["S6_BEHAVIOUR_IF_STAGE2_FAILS"] == "2"
    # /init must start as root (stage-2 remaps ids); every program then drops to `indus`.
    assert not any(word == "USER" for word, _ in final)


def test_the_tree_carries_the_docker_install_stamp() -> None:
    build = " ".join(rest for word, rest in stages(PRODUCT)["build"] if word == "RUN")
    assert "printf 'docker\\n' > /opt/indus/.install_method" in build


def test_s6_and_the_entry_scripts_are_installed() -> None:
    final = stages(PRODUCT)["product"]
    copies = [rest for word, rest in final if word == "COPY"]
    assert any("--from=fetch /out/s6root/ /" in c for c in copies)
    assert any(
        "docker/entrypoint.sh docker/stage2.sh docker/main-wrapper.sh /opt/indus/docker/" in c
        for c in copies
    )
    assert any("docker/s6/ /etc/s6-overlay/s6-rc.d/" in c for c in copies)


def test_python_and_dependencies_come_from_the_lock() -> None:
    build = stages(PRODUCT)["build"]
    runs = " ".join(rest for word, rest in build if word == "RUN")
    assert 'uv python install "${PYTHON_VERSION}"' in runs
    assert runs.count("uv sync --frozen --no-dev") == 2
    assert "--no-install-project" in runs
    assert "ARG PYTHON_VERSION=3.14" in PRODUCT
    assert "indus.web.bundle publish" in runs


def test_browser_variant_adds_chromium_and_the_browser_extra() -> None:
    final = " ".join(rest for word, rest in stages(PRODUCT)["product"] if word == "RUN")
    assert '[ "${VARIANT}" = browser ]' in final and "chromium" in final
    build = " ".join(rest for word, rest in stages(PRODUCT)["build"] if word == "RUN")
    assert 'extras="${extras} browser"' in build
    assert "ARG VARIANT=slim" in PRODUCT


def test_every_download_goes_through_the_pins_script() -> None:
    for text in (PRODUCT, SANDBOX):
        for word, rest in instructions(text):
            assert not (word == "ADD" and "://" in rest), rest
            if word == "RUN":
                assert not re.search(r"\b(curl|wget)\s+(-|https?://)", rest), rest
                assert "| sh" not in rest and "| bash" not in rest
    fetch = " ".join(rest for word, rest in stages(PRODUCT)["fetch"] if word == "RUN")
    for name in ("uv", "s6-overlay", "node"):
        assert f"pins.py --root /src fetch {name} " in fetch


# -- sandbox image --------------------------------------------------------------------------------


def test_sandbox_targets_and_tools() -> None:
    sand = stages(SANDBOX)
    assert list(sand) == ["slim", "browser"]
    assert sand["browser"][0] == ("FROM", "slim AS browser")
    slim_run = " ".join(rest for word, rest in sand["slim"] if word == "RUN")
    for package in ("bash", "coreutils", "git", "curl", "python3", "build-essential", "ripgrep"):
        assert re.search(rf"(?<![\w-]){re.escape(package)}(?![\w-])", slim_run), package
    browser_run = " ".join(rest for word, rest in sand["browser"] if word == "RUN")
    assert "chromium" in browser_run and "xvfb" in browser_run


def test_sandbox_user_labels_and_no_entrypoint() -> None:
    sand = stages(SANDBOX)
    slim = sand["slim"]
    run = " ".join(rest for word, rest in slim if word == "RUN")
    assert "--uid 10000" in run
    labels = " ".join(rest for word, rest in slim if word == "LABEL")
    for key in ("title", "description", "licenses", "source"):
        assert f"org.opencontainers.image.{key}=" in labels
    assert not any(word == "ENTRYPOINT" for word, _ in instructions(SANDBOX))
    assert not any(word == "USER" for word, _ in instructions(SANDBOX))


def test_sandbox_sudo_is_off_unless_asked() -> None:
    assert "ARG SANDBOX_SUDO=0" in SANDBOX
    run = " ".join(rest for word, rest in stages(SANDBOX)["slim"] if word == "RUN")
    # sudo is installed and granted only inside `if [ "${SANDBOX_SUDO}" = 1 ]` branches.
    guard = 'if [ "${SANDBOX_SUDO}" = 1 ]; then'
    blocks = [b for b in run.split("fi;") if re.search(r"(?<!SANDBOX_)\bsudo", b)]
    assert len(blocks) == 2
    for block in blocks:
        first = re.search(r"(?<!SANDBOX_)\bsudo", block)
        assert first is not None and guard in block[: first.start()], block


# -- context and naming ---------------------------------------------------------------------------


def dockerignore() -> list[str]:
    lines = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")]


@pytest.mark.parametrize(
    "pattern", [".git", ".venv", "**/node_modules", "**/__pycache__", "tests", "docs", "**/.env"]
)
def test_dockerignore_keeps_out(pattern: str) -> None:
    assert pattern in dockerignore()


def test_the_web_stage_gets_the_user_guides_it_bundles() -> None:
    # The dashboard's Docs page imports docs/user and mkdocs.yml at build time (W58-J2-1).
    assert "!docs/user" in dockerignore()
    copies = [rest for word, rest in stages(PRODUCT)["web"] if word == "COPY"]
    assert "docs/user/ docs/user/" in copies and "mkdocs.yml ./" in copies


def test_dockerignore_keeps_what_the_build_copies() -> None:
    ignored = set(dockerignore())
    for needed in (
        "README.md",
        "LICENSE",
        "src",
        "skills",
        "locales",
        "scripts",
        "apps",
        "packages",
    ):
        assert needed not in ignored
    copied = re.findall(r"^COPY (?!--from)(?:--\S+ )*(.+) \S+$", PRODUCT, flags=re.MULTILINE)
    for line in copied:
        for source in line.split():
            assert (ROOT / source.rstrip("/")).exists() or source.startswith("scripts/install"), (
                source
            )


def test_no_reference_names_in_any_docker_file() -> None:
    texts = {str(p.relative_to(ROOT)): p.read_text(encoding="utf-8") for p in DOCKER_FILES}
    assert banned_hits(texts) == []


def test_every_docker_shell_script_is_posix_sh_and_executable() -> None:
    scripts = [p for p in DOCKER_FILES if p.name.endswith(".sh") or p.name in ("run", "finish")]
    assert len(scripts) >= 7
    for path in scripts:
        assert path.read_text(encoding="utf-8").startswith("#!/bin/sh\n"), path
        assert path.stat().st_mode & 0o111, f"{path} is not executable"


def test_s6_service_tree() -> None:
    s6 = ROOT / "docker" / "s6"
    assert (s6 / "indus-prepare" / "type").read_text().strip() == "oneshot"
    assert (s6 / "indus-prepare" / "up").read_text().strip() == "/opt/indus/docker/stage2.sh"
    for name in ("gateway", "dashboard"):
        assert (s6 / name / "type").read_text().strip() == "longrun"
        assert (s6 / name / "dependencies.d" / "indus-prepare").is_file()
        assert (s6 / name / "run").is_file() and (s6 / name / "finish").is_file()
    assert sorted(p.name for p in (s6 / "user" / "contents.d").iterdir()) == [
        "dashboard",
        "gateway",
        "indus-prepare",
    ]


def test_extras_named_in_the_dockerfile_exist() -> None:
    import tomllib

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    extras = set(project["optional-dependencies"])
    m = re.search(r'ARG INDUS_EXTRAS="([^"]+)"', PRODUCT)
    assert m is not None
    for name in [*m.group(1).split(), "browser"]:
        assert name in extras, name


def test_docker_files_are_all_tracked_here() -> None:
    names = {str(p.relative_to(ROOT)) for p in DOCKER_FILES}
    assert {"docker/Dockerfile", "docker/sandbox/Dockerfile", "docker/pins.json"} <= names
    assert all(isinstance(p, Path) for p in DOCKER_FILES)
