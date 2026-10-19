"""P15-30: ``indus gateway api-key`` create / list / revoke (non-TTY)."""

from __future__ import annotations

import json

from click.testing import CliRunner

from indus.cli.args import CliState
from indus.cli.commands._support import secret_store
from indus.cli.commands.gateway_api_key import api_key_group
from indus.foundation.scope import Scope
from indus.state.api_keys import ApiKeyRing


def _run(scope: Scope, *args: str) -> tuple[int, str, str]:
    state = CliState(scope=scope, stdin_isatty=lambda: False, stdout_isatty=lambda: False)
    result = CliRunner().invoke(api_key_group, list(args), obj=state)
    return result.exit_code, result.stdout, result.stderr


def test_create_shows_the_key_once_and_list_never_shows_it(scope: Scope) -> None:
    code, out, _ = _run(scope)
    assert code == 0 and "No API keys yet" in out
    code, out, _ = _run(scope, "create", "--label", "open-webui")
    assert code == 0 and "Created API key" in out
    (key,) = ApiKeyRing(scope, secret_store(scope)).list()
    value = secret_store(scope).get(f"INDUS_API_KEY_{key.key_id}")
    assert value and value in out
    code, listed, _ = _run(scope, "list")
    assert code == 0 and key.key_id in listed and "open-webui" in listed
    assert value not in listed
    data = json.loads(_run(scope, "list", "--json")[1])
    assert data["keys"][0]["id"] == key.key_id and value not in json.dumps(data)


def test_revoke(scope: Scope) -> None:
    _run(scope, "create")
    (key,) = ApiKeyRing(scope, secret_store(scope)).list()
    code, out, _ = _run(scope, "revoke", key.key_id)
    assert code == 0 and "Revoked" in out
    assert ApiKeyRing(scope, secret_store(scope)).list() == []
    code, _, err = _run(scope, "revoke", key.key_id)
    assert code == 1 and "no key" in err
    assert _run(scope, "revoke")[0] == 2
