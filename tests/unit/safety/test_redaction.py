"""Tool-output redaction on top of the P01 redactor (P05-06).

Every secret here is assembled at run time from harmless pieces; nothing in this file looks like a
real vendor credential.
"""

from __future__ import annotations

import random
from collections.abc import Mapping
from pathlib import Path

import pytest

from indus.foundation.redact import MASK_GAP
from indus.foundation.scope import Scope, make_scope, use_scope
from indus.safety import redaction as rd
from indus.tools import spill

_RNG = random.Random(5506)
_ALNUM = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def body(n: int) -> str:
    return "".join(_RNG.choice(_ALNUM) for _ in range(n))


def vendor_key() -> str:
    return "s" + "k-" + body(32)


def forge_token() -> str:
    return "gh" + "p_" + body(36)


def chat_token() -> str:
    return "xo" + "xb-" + body(24)


def jwt() -> str:
    return "ey" + "J" + body(20) + "." + "ey" + "J" + body(30) + "." + body(25)


class FakeSecrets:
    def __init__(self, values: Mapping[str, str]) -> None:
        self._values = dict(values)

    def get(self, name: str, /) -> str | None:
        return self._values.get(name)

    def known_values(self) -> list[str]:
        return list(self._values.values())

    @property
    def generation(self) -> int:
        return 1


def scope_for(
    tmp_path: Path, *, env: Mapping[str, str] | None = None, secrets: Mapping[str, str] = {}
) -> Scope:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return make_scope(home, env=env or {}).with_secrets(FakeSecrets(secrets))


OFF = {"INDUS_REDACT_SECRETS": "false"}


# -- shapes -------------------------------------------------------------------------------------


def test_shape_families_masked(tmp_path: Path) -> None:
    scope = scope_for(tmp_path)
    secrets = {
        "vendor": vendor_key(),
        "forge": forge_token(),
        "chat": chat_token(),
        "jwt": jwt(),
        "bearer": body(40),
        "urlpass": body(14),
    }
    text = "\n".join(
        [
            f"using key {secrets['vendor']} now",
            f"remote token={secrets['forge']}",
            f"chat {secrets['chat']}",
            f"session {secrets['jwt']}",
            f"Authorization: Bearer {secrets['bearer']}",
            f"clone https://deploy:{secrets['urlpass']}@git.example.test/r.git",
        ]
    )
    out = rd.redact_output(text, command="make deploy", scope=scope)
    for value in secrets.values():
        assert value not in out
    assert MASK_GAP in out


def test_private_key_block_masked(tmp_path: Path) -> None:
    inner = "\n".join(body(64) for _ in range(4))
    block = "-----BEGIN " + "PRIVATE KEY-----\n" + inner + "\n-----END " + "PRIVATE KEY-----"
    out = rd.redact_output(
        "key:\n" + block + "\ndone", command="cat id_test", scope=scope_for(tmp_path)
    )
    assert inner not in out and out.endswith("done")


def test_secret_headers_masked(tmp_path: Path) -> None:
    value = body(30)
    for header in ("x-api-key", "X-Auth-Token", "Private-Token", "cookie"):
        out = rd.redact_output(
            f"> {header}: {value}\n", command="curl -v x", scope=scope_for(tmp_path)
        )
        assert value not in out, header
    kept = "Content-Type: application/json"
    assert rd.redact_output(kept, command="curl -v x", scope=scope_for(tmp_path)) == kept


# -- exact values -------------------------------------------------------------------------------


def test_scope_secret_value_masked_anywhere(tmp_path: Path) -> None:
    stored = "plainlooking" + body(10)
    scope = scope_for(tmp_path, secrets={"SERVICE_PASS": stored})
    text = f"prefix{stored}suffix\nline two {stored}\n{{'v': '{stored}'}}"
    out = rd.redact_output(text, command="python report.py", scope=scope)
    assert stored not in out
    assert out.count(MASK_GAP) >= 3 or "[redacted]" in out


def test_env_file_value_masked(tmp_path: Path) -> None:
    scope = scope_for(tmp_path)
    value = "dotenvvalue" + body(12)
    (scope.home / ".env").write_text(f"MY_SERVICE_PASS={value}\n", encoding="utf-8")
    out = rd.redact_output(f"connected with {value}", command="./run", scope=scope)
    assert value not in out


# -- command awareness --------------------------------------------------------------------------

DUMP = "MY_SERVICE_TOKEN={v}\nHOME=/home/u\nAPP_SECRET='{w}'\n"


@pytest.mark.parametrize(
    "command",
    [
        "env",
        "printenv",
        "export -p",
        "export",
        "set",
        "declare -x",
        "sudo env",
        "FOO=1 env",
        "env -0",
        "ls; env | sort",
        "printenv | grep TOKEN",
        "cat .env",
        "cat ./config/.env.local",
        "head -n 5 ~/.bashrc",
        "grep TOKEN .env",
        "tail ~/.aws/credentials",
        "cat ~/.indus/config.yaml",
        "cat $INDUS_HOME/auth.json",
        "sed -n 1,5p ~/.netrc",
        "wc -l < .env",
    ],
)
def test_dump_commands_get_assignment_pass(tmp_path: Path, command: str) -> None:
    v, w = body(20), body(16)
    out = rd.redact_output(DUMP.format(v=v, w=w), command=command, scope=scope_for(tmp_path))
    assert v not in out and w not in out, command
    assert "HOME=/home/u" in out


@pytest.mark.parametrize(
    "command",
    [
        "cat app.py",
        "grep .env app.py",
        "env FOO=1 python app.py",
        "export FOO=1",
        "set -euo pipefail",
        "awk '{print $1; print $2}' settings.py",
        "cat /srv/app/config.yaml",
        "less tokens.py",
    ],
)
def test_other_commands_keep_code_readable(tmp_path: Path, command: str) -> None:
    code = (
        "MAX_TOKEN = 4096\n"
        "token = make_token(user)\n"
        'API_KEY = os.getenv("API_KEY")\n'
        'password = "changeme-in-tests"\n'
    )
    out = rd.redact_output(code, command=command, scope=scope_for(tmp_path))
    assert out == code, command


def test_bot_token_shape_masked(tmp_path: Path) -> None:
    secret = body(35)
    out = rd.redact_output(
        f"bot url /bot123456789:{secret}/getMe", command="ls", scope=scope_for(tmp_path)
    )
    assert secret not in out and "123456789:" in out


def test_diagnostic_lines_get_assignment_pass_in_code_mode(tmp_path: Path) -> None:
    v, w = body(18), body(18)
    output = (
        "def test_x():\n"
        "    token = make_token()\n"
        f"E       AssertionError: assert {{'SERVICE_TOKEN': '{v}'}} == {{}}\n"
        f"KeyError: API_SECRET={w}\n"
    )
    out = rd.redact_output(output, command="pytest -q", scope=scope_for(tmp_path))
    assert v not in out and w not in out
    assert "    token = make_token()" in out


def test_code_output_still_masks_shapes(tmp_path: Path) -> None:
    key = vendor_key()
    code = f'MAX_TOKEN = 4096\nCLIENT = Client(api_key="{key}")\n'
    out = rd.redact_output(code, command="cat app.py", scope=scope_for(tmp_path))
    assert key not in out and "MAX_TOKEN = 4096" in out


def test_unknown_source_gets_assignment_pass(tmp_path: Path) -> None:
    v = body(20)
    out = rd.redact_output(f"DB_PASSWORD={v}", scope=scope_for(tmp_path))
    assert v not in out


def test_getenv_line_readable_even_in_dump_mode(tmp_path: Path) -> None:
    line = 'API_KEY = os.getenv("API_KEY")'
    assert rd.redact_output(line, command="cat .env", scope=scope_for(tmp_path)) == line


@pytest.mark.parametrize(
    ("command", "dump"),
    [
        ("env", True),
        ("env -i PATH=/bin ls", False),
        ("set", True),
        ("set -x", False),
        ("declare -p", True),
        ("declare -x FOO=1", False),
        ("typeset", True),
        ("echo 'env'", False),
        ("echo 'a | env'", False),
        ("", False),
        (None, False),
    ],
)
def test_is_env_dump(command: str | None, dump: bool) -> None:
    assert rd.is_env_dump(command) is dump


def test_reads_secret_file_under_scope_home(tmp_path: Path) -> None:
    home = tmp_path / "custom-home"
    home.mkdir()
    assert rd.reads_secret_file(f"cat {home}/config.yaml", indus_home=home)
    assert not rd.reads_secret_file(f"cat {tmp_path}/other/config.yaml", indus_home=home)
    assert not rd.reads_secret_file("cat credentials")
    assert rd.reads_secret_file("cat prod.env")
    assert not rd.reads_secret_file("grep -r .bashrc src")


# -- switch and force ---------------------------------------------------------------------------


def test_switch_off_leaves_output_alone(tmp_path: Path) -> None:
    key = vendor_key()
    scope = scope_for(tmp_path, env=OFF)
    assert rd.redact_output(f"k={key}", command="env", scope=scope) == f"k={key}"


def test_force_masks_with_switch_off(tmp_path: Path) -> None:
    key = vendor_key()
    stored = "storedsecret" + body(8)
    scope = scope_for(tmp_path, env=OFF, secrets={"S": stored})
    out = rd.redact_output(f"{key} {stored}", command="env", scope=scope, force=True)
    assert key not in out and stored not in out


def test_current_scope_is_used(tmp_path: Path) -> None:
    stored = "contextsecret" + body(8)
    scope = scope_for(tmp_path, secrets={"S": stored})
    with use_scope(scope):
        assert stored not in rd.redact_output(f"value {stored}", command="ls")


def test_no_scope_masks_shapes_only(tmp_path: Path) -> None:
    key = forge_token()
    assert key not in rd.redact_output(f"t {key}", command="ls")


def test_empty_text() -> None:
    assert rd.redact_output("") == ""


def test_failure_withholds_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> str:
        raise RuntimeError("broken")

    monkeypatch.setattr(rd.Redactor, "redact", boom)
    assert rd.redact_output("anything", scope=scope_for(tmp_path)) == rd.WITHHELD


def test_spill_files_are_force_redacted(tmp_path: Path) -> None:
    key = vendor_key()
    stored = "spillsecret" + body(10)
    scope = scope_for(tmp_path, env=OFF, secrets={"S": stored})
    sink = spill.host_spill_for(scope, lambda: set())
    path = sink.save("call_1.txt", f"big output {key} and {stored}")
    assert path is not None
    written = Path(path).read_text(encoding="utf-8")
    assert key not in written and stored not in written and "big output" in written


def test_redactor_cache_follows_scope(tmp_path: Path) -> None:
    a = scope_for(tmp_path, secrets={"S": "firstsecret1234"})
    b = make_scope(tmp_path / "b", env={}).with_secrets(FakeSecrets({"S": "secondsecret567"}))
    assert "firstsecret1234" not in rd.redact_output("firstsecret1234", scope=a)
    assert rd.redact_output("firstsecret1234 x", command="ls", scope=b) == "firstsecret1234 x"
    assert "secondsecret567" not in rd.redact_output("secondsecret567", scope=b)


def test_seeded_random_output_never_leaks_stored_values(tmp_path: Path) -> None:
    rng = random.Random(606)
    stored = [body(rng.randint(8, 40)) for _ in range(6)]
    scope = scope_for(tmp_path, secrets={f"S{i}": v for i, v in enumerate(stored)})
    for _ in range(200):
        parts = [rng.choice([*stored, body(5), " ", "=", "\n", "KEY=", '"'])]
        parts += [rng.choice([*stored, body(3), ":", "/", "@"]) for _ in range(rng.randint(1, 8))]
        text = "".join(parts)
        out = rd.redact_output(text, command=rng.choice(["env", "cat a.py", None]), scope=scope)
        assert not any(v in out for v in stored), text


def test_redact_arguments_masks_strings_and_secret_keys(scope: Scope) -> None:
    token = "ghp_" + "Q" * 36
    raw = {
        "command": f"curl -H 'Authorization: Bearer {token}' https://example.invalid",
        "api_key": "plain-value-123",
        "nested": [{"text": f"key={token}"}],
        "count": 3,
    }
    shown = rd.redact_arguments(raw, scope=scope)
    assert token not in repr(shown)
    assert shown["api_key"] != "plain-value-123"
    assert shown["count"] == 3 and shown["command"].startswith("curl -H")
    assert raw["api_key"] == "plain-value-123"  # the caller's mapping is untouched
