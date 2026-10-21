"""P25-15: the generated reference pages are produced from the code, stay fresh, and cover it."""

from __future__ import annotations

import re
from pathlib import Path

import click
import pytest

from indus.cli.app import cli
from indus.foundation.config import schema
from indus.protocol import errors, events, methods
from indus.service.commands.registry import REGISTRY
from indus.tools import toolsets
from scripts.docs import gen_reference as gen

REFERENCE = gen.OUT_DIR


@pytest.fixture(scope="module")
def pages() -> dict[str, str]:
    return gen.render_all()


def test_every_page_is_produced_with_the_marker(pages: dict[str, str]) -> None:
    assert list(pages) == list(gen.RENDERERS)
    for name, text in pages.items():
        assert text.startswith(gen.MARKER + "\n"), name
        assert text.endswith("\n") and not text.endswith("\n\n"), name


def test_rendering_twice_gives_identical_bytes(pages: dict[str, str]) -> None:
    assert gen.render_all() == pages


def test_committed_pages_are_fresh(pages: dict[str, str]) -> None:
    assert gen.stale_report(REFERENCE, pages) == []


def test_the_hand_written_page_is_left_alone(tmp_path: Path, pages: dict[str, str]) -> None:
    own = tmp_path / "trajectory-format.md"
    own.write_text("# Ours\n", encoding="utf-8")
    gen.write_pages(tmp_path, pages)
    assert own.read_text(encoding="utf-8") == "# Ours\n"
    assert gen.stale_report(tmp_path, pages) == []


def test_written_pages_check_clean_and_a_second_write_touches_nothing(
    tmp_path: Path, pages: dict[str, str]
) -> None:
    assert gen.write_pages(tmp_path, pages) == list(pages)
    assert gen.write_pages(tmp_path, pages) == []
    assert gen.main(["--check", "--out", str(tmp_path)]) == 0


def _scratch_group(with_extra_option: bool) -> click.Group:
    @click.group()
    def root() -> None:
        """A scratch tool."""

    @root.command()
    @click.option("--size", type=int, default=3, help="How many.")
    def run(size: int) -> None:
        """Run it."""

    if with_extra_option:
        run.params.append(click.Option(["--fast"], is_flag=True, help="Go faster."))
    return root


def test_a_new_cli_option_makes_the_page_stale(tmp_path: Path) -> None:
    before = {"cli.md": gen.render_cli(_scratch_group(False), "scratch")}
    gen.write_pages(tmp_path, before)
    after = {"cli.md": gen.render_cli(_scratch_group(True), "scratch")}
    assert "`--fast`" in after["cli.md"] and "`--fast`" not in before["cli.md"]
    assert gen.stale_report(tmp_path, after) == ["cli.md: stale"]


def test_check_fails_on_a_stale_missing_or_orphaned_page(
    tmp_path: Path, pages: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    gen.write_pages(tmp_path, pages)
    (tmp_path / "tools.md").write_text(pages["tools.md"] + "edited\n", encoding="utf-8")
    (tmp_path / "skills.md").unlink()
    (tmp_path / "old-page.md").write_text(gen.MARKER + "\n# Old\n", encoding="utf-8")
    assert gen.main(["--check", "--out", str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "tools.md: stale" in err
    assert "skills.md: missing" in err
    assert "old-page.md: generated page no longer produced" in err
    gen.write_pages(tmp_path, pages)
    assert not (tmp_path / "old-page.md").exists()


def test_every_registered_tool_and_toolset_appears(pages: dict[str, str]) -> None:
    from indus.learning.host import tool_registry

    text = pages["tools.md"]
    for name in tool_registry().names():
        assert f"### {name}\n" in text, name
    for name in gen.scan_tools().entries:
        assert f"### {name}\n" in text, name
    for name in toolsets.TOOLSETS:
        assert f"| `{name}` |" in text, name


def test_tool_discovery_finds_every_register_tools_module() -> None:
    modules = gen.tool_modules()
    assert "indus.tools.builtin.terminal" in modules
    assert "indus.learning.memory.tool" in modules
    assert "indus.cron.tool" in modules
    assert gen.scan_tools().failed == {}


def test_every_slash_command_appears(pages: dict[str, str]) -> None:
    text = pages["slash-commands.md"]
    for cmd in REGISTRY:
        assert f"| `/{cmd.name}` |" in text, cmd.name
        for alias in cmd.aliases:
            assert f"`/{alias}`" in text, alias


def test_every_top_level_command_and_subcommand_appears(pages: dict[str, str]) -> None:
    text = pages["cli.md"]
    ctx = click.Context(cli, info_name="indus")
    for name in cli.list_commands(ctx):
        command = cli.get_command(ctx, name)
        assert command is not None
        if command.hidden:
            assert f"## indus {name}\n" not in text
            continue
        assert f"## indus {name}\n" in text, name
        if isinstance(command, click.Group):
            sub_ctx = click.Context(command, info_name=name, parent=ctx)
            for sub in command.list_commands(sub_ctx):
                found = command.get_command(sub_ctx, sub)
                if found is not None and not found.hidden:
                    assert f"### indus {name} {sub}\n" in text, f"{name} {sub}"


def test_every_config_section_and_leaf_key_appears(pages: dict[str, str]) -> None:
    text = pages["configuration.md"]
    for name, info in schema.ConfigModel.model_fields.items():
        key = info.alias or name
        if key == schema.VERSION_KEY:
            continue
        assert f"## {key}\n" in text, key
    assert "| `agent.max_turns` | int | `150` |" in text


def test_protocol_methods_events_and_every_error_code_appear(pages: dict[str, str]) -> None:
    text = pages["protocol.md"]
    for name in methods.METHODS:
        assert f"### {name}\n" in text, name
    for name in events.EVENTS:
        assert f"### {name}\n" in text, name
    for error in errors.ERROR_TABLE:
        assert f"| {error.code} | `{error.name}` |" in text, error.name


def test_every_mcp_serve_tool_appears(pages: dict[str, str]) -> None:
    from indus.mcp_serve.server import APPROVAL_TOOLS, TOOL_ORDER

    text = pages["mcp-serve-tools.md"]
    for name in (*TOOL_ORDER, *APPROVAL_TOOLS):
        assert f"## {name}\n" in text, name


def test_every_starter_skill_appears(pages: dict[str, str]) -> None:
    text = pages["skills.md"]
    found = sorted(gen.SKILLS_DIR.rglob("SKILL.md"))
    assert found
    for path in found:
        assert f"| `{path.parent.name}` |" in text, path.parent.name


def test_environment_page_lists_indus_names_and_platform_credentials(
    pages: dict[str, str],
) -> None:
    text = pages["environment-variables.md"]
    assert "| `INDUS_HOME` |" in text
    assert "| `TELEGRAM_BOT_TOKEN` | Telegram | credential |" in text
    assert "| `OPENAI_API_KEY` |" in text
    for name in gen.scan_env().indus:
        assert f"`{name}`" in text, name


def test_env_scan_reads_constants_and_env_lookups(tmp_path: Path) -> None:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "mod.py").write_text(
        "import os\n"
        "A = 'INDUS_FIRST'\n"
        "PREFIX = 'INDUS_FAMILY_'\n"
        "b = os.environ.get('VENDOR_ONE')\n"
        "c = os.getenv('VENDOR_TWO')\n"
        "d = env['VENDOR_THREE']\n"
        "e = {}.get('NOT_ENV')\n"
        "f = 'lowercase_ok'\n",
        encoding="utf-8",
    )
    found = gen.scan_env(pkg)
    assert set(found.indus) == {"INDUS_FIRST"}
    assert set(found.prefixes) == {"INDUS_FAMILY_"}
    assert set(found.other) == {"VENDOR_ONE", "VENDOR_TWO", "VENDOR_THREE"}
    assert found.other["VENDOR_ONE"] == {"pkg.mod"}


def test_output_holds_no_machine_paths(pages: dict[str, str]) -> None:
    home = str(Path.home())
    for name, text in pages.items():
        assert str(gen.ROOT) not in text, name
        assert home == "/" or home not in text, name


@pytest.mark.parametrize(
    ("value", "shown"),
    [
        (None, "null"),
        (True, "true"),
        ("", '""'),
        ([1, 2], "[1, 2]"),
        ({"a": 1}, "{a: 1}"),
        (frozenset({"b", "a"}), "[a, b]"),
        (list(range(100)), "(a list of 100 items)"),
    ],
)
def test_show_value(value: object, shown: str) -> None:
    assert gen.show_value(value) == shown


def test_cells_escape_pipes_and_newlines() -> None:
    assert gen.cell("a | b\nc") == "a \\| b c"
    assert gen.code("x`y") == "`` x`y ``"


def test_headings_match_their_anchor_links(pages: dict[str, str]) -> None:
    for name, text in pages.items():
        headings = {gen.slug(h) for h in re.findall(r"^#{2,3} (.+)$", text, re.M)}
        for anchor in re.findall(r"\]\(#([^)]+)\)", text):
            assert anchor in headings, f"{name}: #{anchor}"
