"""The P01 "done when" check: one ``work`` scope from bootstrap, config, secrets and logs."""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from indus.foundation import bootstrap, config, layout
from indus.foundation import log as logmod
from indus.foundation.config import loader
from indus.foundation.secrets import SecretStore

# Matches no credential shape, so only the secret store can get it masked.
KEY_VALUE = "zq81PLAINvalue7731abcd"


@pytest.fixture(autouse=True)
def clean_process_state() -> Iterator[None]:
    root = logging.getLogger()
    handlers = list(root.handlers)
    level = root.level
    factory = logging.getLogRecordFactory()
    path = list(sys.path)
    layout.reset_memo()
    loader.reset()
    yield
    logmod.shutdown()
    root.handlers[:] = handlers
    root.setLevel(level)
    logging.setLogRecordFactory(factory)
    sys.path[:] = path
    layout.reset_memo()
    loader.reset()


def test_work_scope_config_secrets_and_masked_logs(tmp_path: Path) -> None:
    root = tmp_path / "root"
    work = root / "profiles" / "work"
    work.mkdir(parents=True)
    (work / "profile.yaml").touch()
    env = {"INDUS_HOME": str(root)}

    scope, argv = bootstrap.run(["-p", "work", "chat"], env)
    assert (scope.profile_id, scope.home, argv) == ("work", work, ["chat"])

    # Config: write with the writer, break the file by hand, still see the deny rule.
    config.save(scope, {"approvals": {"deny": ["rm"]}})
    assert config.load(scope).get("approvals.deny") == ["rm"]
    path = config.config_path(scope)
    path.write_text("approvals: [rm\n", encoding="utf-8")
    assert config.load(scope).get("approvals.deny") == ["rm"]
    loader.reset()  # as a fresh process would: the last good copy comes from the backup
    assert config.load(scope).get("approvals.deny") == ["rm"]

    # Secrets: the scope carries the profile's store.
    store = scope.secrets
    assert isinstance(store, SecretStore)
    store.set("OPENAI_API_KEY", KEY_VALUE)
    assert store.get("OPENAI_API_KEY") == KEY_VALUE

    # Logs: the line lands in work's agent.log with the stored value masked.
    logging.getLogger("indus.providers.check").info("calling the provider with %s", KEY_VALUE)
    logmod.flush()
    text = (work / "logs" / "agent.log").read_text(encoding="utf-8")
    assert "calling the provider with" in text
    assert KEY_VALUE not in text
    assert not (root / "logs" / "agent.log").exists()
