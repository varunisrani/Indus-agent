from __future__ import annotations

import logging
import multiprocessing
import os
import random
import stat
import string
from pathlib import Path

import pytest

from indus.foundation import envfile
from indus.foundation.envfile import (
    EnvNameDenied,
    EnvNameError,
    custom_key_name,
    parse_line,
    parse_text,
    quote_value,
    read_env_file,
    read_written_names,
    set_values,
)


@pytest.fixture
def env_path(tmp_path: Path) -> Path:
    return tmp_path / ".env"


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# -- reader ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("K=v", ("K", "v")),
        ("export K=v", ("K", "v")),
        ("export\tK=v", ("K", "v")),
        ("K = v", ("K", "v")),
        ("  K=v  ", ("K", "v")),
        ("K=", ("K", "")),
        ('K="a b"', ("K", "a b")),
        ("K='a b'", ("K", "a b")),
        ("K='a \\\" b'", ("K", 'a \\" b')),
        ('K="say \\"hi\\""', ("K", 'say "hi"')),
        ('K="back\\\\slash"', ("K", "back\\slash")),
        ('K="x" # note', ("K", "x")),
        ("K=a#b", ("K", "a#b")),
        ("K=a #b", ("K", "a")),
        ("K= # only a comment", ("K", "")),
        ("K=#hash", ("K", "#hash")),
        ("K==x", ("K", "=x")),
        ("﻿K=v", ("K", "v")),
        ("# K=v", None),
        ("", None),
        ("   ", None),
        ("just words", None),
        ("export K", None),
        ("1K=v", None),
        ("K-Y=v", None),
        ("exportK=v", ("exportK", "v")),
    ],
)
def test_parse_line_shapes(line: str, expected: tuple[str, str] | None) -> None:
    assert parse_line(line) == expected


def test_parse_text_last_assignment_wins() -> None:
    assert parse_text("A=1\nB=2\nA=3\n") == {"A": "3", "B": "2"}


def test_read_missing_file_is_empty(env_path: Path) -> None:
    assert read_env_file(env_path) == {}


def test_read_strips_bom_and_nul_with_warning(
    env_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    env_path.write_bytes(b"\xef\xbb\xbfFIRST=1\nSEC\x00OND=2\n")
    with caplog.at_level(logging.WARNING, logger="indus.foundation.envfile"):
        assert read_env_file(env_path) == {"FIRST": "1", "SECOND": "2"}
    assert any("NUL" in r.getMessage() for r in caplog.records)


# -- writer: round trip and line shapes --------------------------------------------------------


def test_round_trip_value_with_space_and_hash(env_path: Path) -> None:
    set_values(env_path, {"K": "a b#c"})
    assert read_env_file(env_path) == {"K": "a b#c"}
    assert env_path.read_text() == 'K="a b#c"\n'


@pytest.mark.parametrize(
    "line",
    ["K=old", "export K=old", "export\tK=old", "K = old", "  K=old", 'K="old"', "K='old' # c"],
)
def test_set_replaces_every_line_shape(env_path: Path, line: str) -> None:
    env_path.write_text(f"# head\nA=1\n{line}\nZ=9\n")
    set_values(env_path, {"K": "new"})
    assert env_path.read_text() == "# head\nA=1\nK=new\nZ=9\n"


@pytest.mark.parametrize(
    "line", ["K=old", "export K=old", "K = old", "K   =   old", 'export K="old value"']
)
def test_delete_removes_every_line_shape(env_path: Path, line: str) -> None:
    env_path.write_text(f"A=1\n{line}\nZ=9\n")
    set_values(env_path, {"K": None})
    assert env_path.read_text() == "A=1\nZ=9\n"
    assert "K" not in read_env_file(env_path)


def test_set_drops_duplicate_definitions(env_path: Path) -> None:
    env_path.write_text("K=one\nA=1\nexport K=two\nK = three\n")
    set_values(env_path, {"K": "new"})
    assert env_path.read_text() == "K=new\nA=1\n"


def test_deleted_spaced_line_does_not_resurrect(env_path: Path) -> None:
    env_path.write_text("K = v\n")
    set_values(env_path, {"K": None})
    set_values(env_path, {"OTHER": "x"})
    assert read_env_file(env_path) == {"OTHER": "x"}


def test_append_adds_missing_final_newline(env_path: Path) -> None:
    env_path.write_text("A=1")
    set_values(env_path, {"B": "2"})
    assert env_path.read_text() == "A=1\nB=2\n"


def test_other_lines_keep_their_bytes(env_path: Path) -> None:
    original = "# comment  with  spaces\r\nA = 'x y'\r\n\r\nexport B=2\n"
    env_path.write_text(original, newline="")
    set_values(env_path, {"B": "3"})
    assert env_path.read_bytes() == b"# comment  with  spaces\r\nA = 'x y'\r\n\r\nB=3\n"


def test_unchanged_write_keeps_file(env_path: Path) -> None:
    env_path.write_text("A=1\n")
    before = env_path.stat().st_ino
    report = set_values(env_path, {"A": "1"})
    assert report.changed == []
    assert env_path.stat().st_ino == before


def test_delete_on_missing_file_creates_nothing(env_path: Path) -> None:
    set_values(env_path, {"K": None})
    assert not env_path.exists()


# -- writer: names -----------------------------------------------------------------------------


DENIED_SAMPLES = [
    "LD_PRELOAD",
    "LD_ANYTHING_NEW",
    "DYLD_INSERT_LIBRARIES",
    "GIT_CONFIG_KEY_3",
    "PATH",
    "SHELL",
    "EDITOR",
    "VISUAL",
    "PAGER",
    "PYTHONPATH",
    "NODE_OPTIONS",
    "GIT_SSH_COMMAND",
    "GIT_DIR",
    "SSH_ASKPASS",
    "BASH_ENV",
    "ENV",
    "PROMPT_COMMAND",
    "INDUS_HOME",
    "INDUS_PROFILE",
    "INDUS_CONFIG_PATH",
    "INDUS_ENV_PATH",
    "INDUS_YOLO_MODE",
    "INDUS_ACCEPT_HOOKS",
    "INDUS_REDACT_SECRETS",
    "INDUS_INTERACTIVE",
]


@pytest.mark.parametrize("name", DENIED_SAMPLES)
def test_denylist_refused(env_path: Path, name: str) -> None:
    with pytest.raises(EnvNameDenied, match="edit the .env file by hand"):
        set_values(env_path, {name: "x"}, windows=False)
    assert not env_path.exists()


def _names(text: str) -> set[str]:
    return set(text.split())


def test_denylist_table_from_spec_is_complete() -> None:
    spec = (
        "PATH SHELL BROWSER EDITOR VISUAL PAGER MANPAGER PYTHONPATH PYTHONHOME PYTHONSTARTUP "
        "NODE_OPTIONS NODE_PATH PERL5OPT RUBYOPT CLASSPATH JAVA_TOOL_OPTIONS GOFLAGS RUSTFLAGS "
        "GIT_SSH_COMMAND GIT_ASKPASS GIT_EDITOR GIT_DIR GIT_WORK_TREE SSH_ASKPASS SUDO_ASKPASS "
        "BASH_ENV ENV ZDOTDIR PROMPT_COMMAND VIMINIT EXINIT INDUS_HOME INDUS_PROFILE INDUS_CONFIG "
        "INDUS_CONFIG_PATH INDUS_ENV INDUS_ENV_PATH INDUS_YOLO_MODE INDUS_ACCEPT_HOOKS "
        "INDUS_REDACT_SECRETS INDUS_INTERACTIVE"
    )
    assert _names(spec) <= envfile.DENIED_NAMES
    assert set(envfile.DENIED_PREFIXES) == {"LD_", "DYLD_", "GIT_CONFIG_"}


def test_denylist_superset_of_reference_names() -> None:
    # Reference names that research 04 §3.14.2 lists beyond the phase table, renamed to ours.
    extra = (
        "PYTHONUSERBASE PERL5LIB RUBYLIB _JAVA_OPTIONS JDK_JAVA_OPTIONS GIT_SSH GIT_EXEC_PATH "
        "GIT_SEQUENCE_EDITOR GIT_PAGER GIT_EXTERNAL_DIFF GIT_PROXY_COMMAND GIT_TEMPLATE_DIR "
        "INDUS_OPTIONAL_MCPS INDUS_EXEC_ASK INDUS_GATEWAY_SESSION INDUS_CRON_SESSION "
        "INDUS_SINGLE_QUERY_SESSION INDUS_SESSION_KEY INDUS_SESSION_PLATFORM"
    )
    assert _names(extra) <= envfile.DENIED_NAMES


@pytest.mark.parametrize("name", ["INDUS_FOO", "INDUS_CUSTOM_X_API_KEY", "OPENAI_API_KEY", "_X"])
def test_allowed_names(env_path: Path, name: str) -> None:
    set_values(env_path, {name: "v"}, windows=False)
    assert read_env_file(env_path) == {name: "v"}


def test_denylist_case_insensitive_on_windows(env_path: Path) -> None:
    for name in ["ld_preload", "Path", "indus_home", "git_config_global"]:
        with pytest.raises(EnvNameDenied):
            set_values(env_path, {name: "x"}, windows=True)
    # POSIX names are case-sensitive: lowercase is a different variable.
    set_values(env_path, {"ld_preload": "x"}, windows=False)
    assert read_env_file(env_path) == {"ld_preload": "x"}


def test_windows_mode_matches_lines_case_insensitively(env_path: Path) -> None:
    env_path.write_text("openai_api_key=old\n")
    set_values(env_path, {"OPENAI_API_KEY": "new"}, windows=True)
    assert env_path.read_text() == "OPENAI_API_KEY=new\n"


@pytest.mark.parametrize("name", ["", "1ABC", "A-B", "A B", "A=B", "ÄPFEL"])
def test_invalid_names_refused(env_path: Path, name: str) -> None:
    with pytest.raises(EnvNameError):
        set_values(env_path, {name: "x"})
    with pytest.raises(EnvNameError):
        set_values(env_path, {name: None})


def test_denied_name_may_still_be_deleted(env_path: Path) -> None:
    env_path.write_text("LD_PRELOAD=/tmp/evil.so\nA=1\n")
    set_values(env_path, {"LD_PRELOAD": None})
    assert env_path.read_text() == "A=1\n"


# -- writer: values ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", ""),
        ("plain", "plain"),
        ("a\\b", "a\\b"),
        ("a b", '"a b"'),
        ("tab\there", '"tab\there"'),
        ("x#y", '"x#y"'),
        ('say "hi"', '"say \\"hi\\""'),
        ("it's", '"it\'s"'),
        ("end\\ ", '"end\\\\ "'),
    ],
)
def test_quote_value_table(value: str, expected: str) -> None:
    assert quote_value(value) == expected
    assert parse_line(f"K={quote_value(value)}") == ("K", value)


def test_newlines_stripped(env_path: Path) -> None:
    set_values(env_path, {"K": "ab\r\ncd\n"})
    assert read_env_file(env_path) == {"K": "abcd"}


def test_non_ascii_stripped_with_positions(
    env_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="indus.foundation.envfile"):
        report = set_values(env_path, {"K": "sk–aébcсерфx"})
    assert read_env_file(env_path) == {"K": "skabcx"}
    assert len(report.warnings) == 1
    message = report.warnings[0]
    assert "positions 2, 4, 7, 8, 9 and 1 more" in message
    assert any(message in r.getMessage() for r in caplog.records)


# -- writer: files -----------------------------------------------------------------------------


def test_new_file_is_owner_only(env_path: Path) -> None:
    set_values(env_path, {"K": "v"})
    assert _mode(env_path) == 0o600


def test_existing_mode_is_kept(env_path: Path) -> None:
    env_path.write_text("A=1\n")
    os.chmod(env_path, 0o640)
    set_values(env_path, {"K": "v"})
    assert _mode(env_path) == 0o640


def test_no_temp_files_left(env_path: Path) -> None:
    set_values(env_path, {"K": "v"})
    set_values(env_path, {"K": None})
    leftovers = [p.name for p in env_path.parent.iterdir() if ".tmp" in p.name]
    assert leftovers == []


def test_written_names_are_recorded(env_path: Path) -> None:
    set_values(env_path, {"A": "1", "B": "2"})
    set_values(env_path, {"A": None, "C": "3"})
    assert read_written_names(env_path) == {"A", "B", "C"}
    record = envfile.keys_record_path(env_path)
    assert record.name == ".env.keys"
    assert "1" not in record.read_text() and "3" not in record.read_text()


def test_custom_key_name() -> None:
    assert custom_key_name("my-host:8080") == "INDUS_CUSTOM_MY_HOST_8080_API_KEY"
    assert custom_key_name("Local LLM") == "INDUS_CUSTOM_LOCAL_LLM_API_KEY"
    assert custom_key_name("9lives") == "INDUS_CUSTOM_9LIVES_API_KEY"
    assert custom_key_name("") == "INDUS_CUSTOM_API_KEY"
    assert custom_key_name("---") == "INDUS_CUSTOM_API_KEY"


def test_printable_ascii_values_round_trip(env_path: Path) -> None:
    # Property check (seeded; becomes a hypothesis @given once the dev dep lands).
    rng = random.Random(1404)
    alphabet = string.printable.replace("\n", "").replace("\r", "").replace("\x0b", "")
    alphabet = alphabet.replace("\x0c", "")
    for _ in range(300):
        value = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40)))
        set_values(env_path, {"K": value, "OTHER": "o"})
        assert read_env_file(env_path) == {"K": value, "OTHER": "o"}, repr(value)


# -- concurrency -------------------------------------------------------------------------------


def _writer(path: str, prefix: str, count: int) -> None:
    from indus.foundation.envfile import set_values as write

    for i in range(count):
        write(Path(path), {f"{prefix}_{i}": str(i)})


@pytest.mark.slow
def test_concurrent_writers_both_land(env_path: Path) -> None:
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=_writer, args=(str(env_path), p, 25)) for p in ("AA", "BB")]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(60)
        assert proc.exitcode == 0
    values = read_env_file(env_path)
    assert {f"AA_{i}" for i in range(25)} | {f"BB_{i}" for i in range(25)} == set(values)
