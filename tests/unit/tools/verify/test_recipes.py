"""P24-17: recipe detection over small workspaces (``tests/fixtures/verify/workspaces``)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from indus.tools.verify.recipes import LOCKFILES, detect_recipe, make_targets, package_manager

WORKSPACES = Path(__file__).resolve().parents[3] / "fixtures" / "verify" / "workspaces"


@pytest.mark.parametrize(
    ("workspace", "kind", "bootstrap", "build", "test", "start", "port"),
    [
        (
            "pnpm_next",
            "nextjs",
            ["pnpm install"],
            ["pnpm build"],
            ["pnpm test", "pnpm lint"],
            "pnpm dev",
            3100,
        ),
        (
            "npm_plain",
            "node",
            ["npm install"],
            ["npm run typecheck"],
            ["npm run check"],
            None,
            None,
        ),
        (
            "uv_fastapi",
            "fastapi",
            ["uv sync"],
            [],
            ["pytest"],
            "uvicorn app:app --host 127.0.0.1 --port 8000",
            8000,
        ),
        (
            "django_site",
            "django",
            ["pip install -r requirements.txt"],
            [],
            ["python manage.py test"],
            "python manage.py runserver 127.0.0.1:8000",
            8000,
        ),
        (
            "plain_python",
            "python",
            ["pip install -r requirements.txt"],
            [],
            ["python -m unittest discover"],
            None,
            None,
        ),
        ("make_test", "make", [], ["make build"], ["make test"], "make serve", None),
        ("go_mod", "go", [], ["go build ./..."], ["go test ./..."], "go run .", None),
        ("rust_lib", "rust", [], ["cargo build"], ["cargo test"], None, None),
        ("gradle_wrapper", "gradle", [], ["./gradlew build"], ["./gradlew test"], None, None),
        ("compose_only", "compose", [], ["docker compose build"], [], "docker compose up", None),
    ],
)
def test_recipe_table(
    workspace: str,
    kind: str,
    bootstrap: list[str],
    build: list[str],
    test: list[str],
    start: str | None,
    port: int | None,
) -> None:
    recipe = detect_recipe(WORKSPACES / workspace)
    assert recipe is not None
    assert (recipe.kind, recipe.bootstrap, recipe.build, recipe.test) == (
        kind,
        bootstrap,
        build,
        test,
    )
    assert (recipe.start, recipe.port) == (start, port)
    assert recipe.as_dict()["kind"] == kind and recipe.evidence


def test_nothing_recognised(tmp_path: Path) -> None:
    assert detect_recipe(WORKSPACES / "empty_dir") is None
    assert detect_recipe(tmp_path / "missing") is None


def test_first_lockfile_wins(tmp_path: Path) -> None:
    assert [m for _, m in LOCKFILES] == [
        "pnpm",
        "bun",
        "bun",
        "yarn",
        "npm",
        "uv",
        "poetry",
        "pipenv",
    ]
    (tmp_path / "yarn.lock").write_text("", encoding="utf-8")
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    assert package_manager(tmp_path) == "yarn"
    (tmp_path / "bun.lockb").write_text("", encoding="utf-8")
    assert package_manager(tmp_path) == "bun"
    (tmp_path / "package.json").write_text('{"scripts": {"test": "bun test"}}', encoding="utf-8")
    recipe = detect_recipe(tmp_path)
    assert recipe is not None
    assert (recipe.bootstrap, recipe.test) == (["bun install"], ["bun run test"])


def test_package_json_wins_over_python_and_make(tmp_path: Path) -> None:
    shutil.copytree(WORKSPACES / "make_test", tmp_path, dirs_exist_ok=True)
    (tmp_path / "requirements.txt").write_text("flask\n", encoding="utf-8")
    flask = detect_recipe(tmp_path)
    assert flask is not None and flask.kind == "flask"
    assert flask.start == "flask --app app.py run --host 127.0.0.1 --port 5000"
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    node = detect_recipe(tmp_path)
    assert node is not None and node.kind == "node" and node.test == []


def test_poetry_and_pyproject_install(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.poetry]\nname='x'\n", encoding="utf-8")
    plain = detect_recipe(tmp_path)
    assert plain is not None and plain.bootstrap == ["pip install -e ."]
    (tmp_path / "poetry.lock").write_text("", encoding="utf-8")
    poetry = detect_recipe(tmp_path)
    assert poetry is not None and poetry.bootstrap == ["poetry install"]


def test_make_targets_ignore_variables_and_specials() -> None:
    text = "CC := gcc\nX = 1\n.PHONY: all\nall: build\nbuild:\n\tcc\nfoo.o: foo.c\n\ttest: no\n"
    assert make_targets(text) == ["all", "build", "foo.o"]


def test_port_from_start_script(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        '{"scripts": {"start": "PORT=4555 node server.js"}, "dependencies": {"vite": "5"}}',
        encoding="utf-8",
    )
    recipe = detect_recipe(tmp_path)
    assert recipe is not None
    assert (recipe.kind, recipe.start, recipe.port) == ("vite", "npm run start", 4555)


def test_unreadable_package_json_falls_through(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("{ not json", encoding="utf-8")
    (tmp_path / "Makefile").write_text("check:\n\t@true\n", encoding="utf-8")
    recipe = detect_recipe(tmp_path)
    assert recipe is not None and (recipe.kind, recipe.test) == ("make", ["make check"])
    (tmp_path / "package.json").write_text("[1, 2]", encoding="utf-8")
    listed = detect_recipe(tmp_path)
    assert listed is not None and listed.kind == "make"
