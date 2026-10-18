"""Dangerous-command detection (P05-07), driven by our own corpus.

The strings below are only ever handed to ``detect``; nothing here runs a command.
"""

from __future__ import annotations

import importlib
import random
import re
import time
from pathlib import Path
from typing import Any

import pytest

from indus.foundation import yamlio
from indus.foundation.scope import make_scope, use_scope
from indus.safety.approvals import SAFE, Detection, detect

# The module shares its name with the function the package exports.
detect_module = importlib.import_module("indus.safety.approvals.detect")

CORPUS = Path(__file__).resolve().parents[3] / "fixtures" / "approvals" / "corpus.yaml"
USER_HOME = "/home/dev"
INDUS_HOME = "/home/dev/.indus"
_CODE_POINT = re.compile(r"\{U\+([0-9A-F]{4,6})\}")


def _expand(text: str) -> str:
    return _CODE_POINT.sub(lambda m: chr(int(m.group(1), 16)), text)


def _rows() -> list[dict[str, Any]]:
    data = yamlio.load_safe(CORPUS)
    return [dict(row) for row in data["cases"]]


def run(command: str) -> Detection:
    return detect(command, user_home=USER_HOME, indus_home=INDUS_HOME)


ROWS = _rows()


def test_corpus_is_large_and_balanced() -> None:
    verdicts = [row["verdict"] for row in ROWS]
    assert len(ROWS) >= 300
    assert verdicts.count("safe") >= 100 and verdicts.count("hardline") >= 50
    commands = [row["cmd"] for row in ROWS]
    assert len(commands) == len(set(commands)), "duplicate corpus rows"
    keys = {row.get("key") for row in ROWS} - {None}
    assert keys <= set(detect_module.DESCRIPTIONS)
    # every category the detector knows has at least one corpus row
    assert set(detect_module.DESCRIPTIONS) - keys <= {"parser_limit"}


@pytest.mark.parametrize("row", ROWS, ids=[f"{i:03d}" for i in range(len(ROWS))])
def test_corpus_verdicts(row: dict[str, Any]) -> None:
    command = _expand(row["cmd"])
    result = run(command)
    verdict = "hardline" if result.hardline else "dangerous" if result.dangerous else "safe"
    assert verdict == row["verdict"], (command, result)
    assert result.pattern_key == row.get("key"), (command, result)
    if result.dangerous:
        assert result.description == detect_module.DESCRIPTIONS[result.pattern_key or ""]
        assert result.trace and result.trace[-1].startswith(result.pattern_key or "")
    else:
        assert result == SAFE


# -- normalisation and obfuscation -----------------------------------------------------------------

DELETE_ROOT = "rm" + " -rf " + "/"


@pytest.mark.parametrize(
    "command",
    [
        "ｒｍ -ｒｆ /",  # fullwidth letters
        "\x1b[31mrm\x1b[0m -rf /",  # ANSI colour around the program
        "\x1b]0;title\x07rm -rf /",  # OSC title sequence
        "\x9b31mrm\x9b0m -rf /",  # 8-bit CSI
        "r\x00m -rf /",  # NUL inside the word
        "\x1b[1mｒｍ\x1b[0m -rf /",
        "rm -rf \\\n/",  # line continuation
        "rm -r\\\nf /",
        "rm ${IFS:0:1}-rf /",
        "rm -rf ${unset_var:-/}",
        "rm -rf ${var/x/}/",
        "r$'\\x6d' -rf /",
        "$'\\x72\\x6d' -rf /",
    ],
)
def test_obfuscated_spellings_hit_the_floor(command: str) -> None:
    result = run(command)
    assert result.hardline and result.pattern_key == "rm_root", (command, result)


def test_obfuscated_disk_writes() -> None:
    assert run("ｄｄ if=/dev/zero of=/dev/sda").pattern_key == "block_device_write"
    assert run("d\x00d if=/dev/sda of=x.img").pattern_key == "disk_tool"
    assert run("ｃｈｍｏｄ 777 /tmp/test").pattern_key == "chmod_world_writable"


def test_lookalikes_stay_safe() -> None:
    assert run("ｌｓ -ｌａ /tmp") == SAFE
    assert run("echo $IFSX") == SAFE
    assert run("echo ${IFS_VALUE}") == SAFE
    assert run("git commit -m 'emoji \U0001f468‍\U0001f4bb ok'") == SAFE


def test_hidden_characters_are_flagged_but_harder_findings_win() -> None:
    soft = run("ls​ -la")
    assert soft.pattern_key == "hidden_unicode" and not soft.hardline
    hard = run("git c​heckout main && r​m -rf /")
    assert hard.hardline and hard.pattern_key == "rm_root"


# -- homes -----------------------------------------------------------------------------------------


def test_home_folding_uses_given_homes() -> None:
    assert detect("rm -rf /srv/me", user_home="/srv/me").pattern_key == "rm_home"
    assert detect("rm -rf /srv/me", user_home="/home/dev").pattern_key == "rm_recursive"
    custom = detect("echo x > /data/indus-home/config.yaml", indus_home="/data/indus-home")
    assert custom.pattern_key == "indus_config_write"
    assert detect("echo x > /data/indus-home/config.yaml", indus_home="/other") == SAFE


def test_degenerate_home_does_not_fold_everything() -> None:
    assert detect("rm -rf /tmp/x", user_home="/").pattern_key == "rm_recursive"
    assert detect("ls /", user_home="") == SAFE


def test_homes_default_to_the_current_scope(tmp_path: Path) -> None:
    home = tmp_path / "indus-home"
    scope = make_scope(home, env={"HOME": str(tmp_path)})
    with use_scope(scope):
        assert detect(f"echo x > {home}/.env").pattern_key == "indus_config_write"
        assert detect(f"rm -rf {tmp_path}").pattern_key == "rm_home"


# -- recursion, limits, malformed input ------------------------------------------------------------


def test_payload_recursion_depth_limit() -> None:
    nested = "ls"
    for _ in range(detect_module.MAX_DEPTH + 2):
        nested = "bash -c " + _quote(nested)
    result = run(nested)
    assert result.hardline and result.pattern_key == "parser_limit"
    shallow = "ls"
    for _ in range(3):
        shallow = "bash -c " + _quote(shallow)
    assert run(shallow) == SAFE


def _quote(text: str) -> str:
    return "'" + text.replace("'", "'\"'\"'") + "'"


def test_size_limits_fail_closed() -> None:
    assert run("echo " + "a" * (detect_module.MAX_CHARS + 1)).pattern_key == "parser_limit"
    assert run("x" * (detect_module.MAX_UNBROKEN + 1)).pattern_key == "parser_limit"
    many = ";".join(["true"] * (detect_module.MAX_SEPARATORS + 2))
    result = run(many)
    assert result.hardline and result.pattern_key == "parser_limit"


def test_hardline_after_many_benign_segments_is_found() -> None:
    command = "; ".join(["echo ok"] * 500) + "; " + DELETE_ROOT
    assert run(command).pattern_key == "rm_root"


def test_malformed_is_hardline_with_reason() -> None:
    result = run("grep 'unterminated")
    assert result.hardline and result.pattern_key == "malformed"
    assert "single quote" in result.trace[-1]


def test_shell_valid_escapes_are_not_malformed() -> None:
    assert run('grep -o "[^\\"]*" file') == SAFE
    assert run("echo 'it'\\''s fine'") == SAFE
    assert run('echo "a\\"b"') == SAFE


# -- trace -----------------------------------------------------------------------------------------


def test_trace_records_the_path() -> None:
    result = run("sudo env nice bash -c 'echo hi; " + DELETE_ROOT + "'")
    steps = " | ".join(result.trace)
    assert "unwrapped sudo" in steps and "unwrapped env" in steps and "unwrapped nice" in steps
    assert "payload of bash -c" in steps
    assert result.trace[-1].startswith("rm_root")


def test_detection_is_a_frozen_value() -> None:
    result = run("reboot")
    with pytest.raises(AttributeError):
        result.dangerous = False  # type: ignore[misc]
    assert Detection(False, False, None, None, ()) == SAFE


# -- robustness ------------------------------------------------------------------------------------

_ALPHABET = list("abcdefrm -/|&;$(){}'\"`\\<>*?~.=\n\t") + ["rm", "-rf", "sudo", "bash -c", "$("]


def test_seeded_random_input_never_raises_and_stays_fast() -> None:
    rng = random.Random(5507)
    started = time.monotonic()
    for _ in range(1500):
        text = "".join(rng.choice(_ALPHABET) for _ in range(rng.randint(0, 60)))
        result = detect(text, user_home=USER_HOME, indus_home=INDUS_HOME)
        assert isinstance(result, Detection)
        if result.hardline:
            assert result.dangerous
    assert time.monotonic() - started < 30


def test_large_heredoc_stays_bounded() -> None:
    body = "\n".join(f"echo 'line {i}' \"quoted {i}\" `date`" for i in range(800))
    started = time.monotonic()
    result = run("bash <<EOF\n" + body + "\nEOF")
    assert result.pattern_key == "heredoc_exec"
    assert time.monotonic() - started < 10
