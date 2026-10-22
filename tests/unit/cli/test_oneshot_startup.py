"""W48-e: the one-shot path does not pay for what a plain run never uses.

``oneshot_first_token`` (tooling/perf/budgets.toml) times ``indus -z`` from process start to the
first reply byte. These checks pin the start-up savings in a fresh interpreter without timing
anything: the wire models build their validators on first use, and ``jsonschema`` (only needed
when a subagent task carries ``output_schema``) is not imported.
"""

from __future__ import annotations

import json
import subprocess
import sys

_PROBE = (
    "import json, sys\n"
    "import indus.cli.oneshot, indus.cli.runtime, indus.core.agent\n"
    "from indus.protocol.methods import SessionCreateParams as Model\n"
    "before = Model.__pydantic_complete__\n"
    "Model(cwd='/work')\n"
    "print(json.dumps({'built_at_import': before, 'built_on_use': Model.__pydantic_complete__,"
    " 'jsonschema': 'jsonschema' in sys.modules}))\n"
)


def _probe() -> dict[str, bool]:
    done = subprocess.run(
        [sys.executable, "-c", _PROBE], capture_output=True, text=True, timeout=120, check=True
    )
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_the_one_shot_imports_build_no_wire_validator_until_one_is_used() -> None:
    seen = _probe()
    assert seen["built_at_import"] is False
    assert seen["built_on_use"] is True


def test_the_one_shot_imports_leave_jsonschema_unloaded() -> None:
    assert _probe()["jsonschema"] is False


_LAZY = (
    "import json, sys\n"
    "import indus.cli.oneshot, indus.cli.runtime, indus.core.agent\n"
    "import indus.learning.skills.project_trust\n"
    "names = ('indus.backends.docker.reaper', 'indus.backends.docker.environment',\n"
    "         'indus.cron.store', 'indus.cron.models', 'indus.safety.content_scan')\n"
    "print(json.dumps(sorted(n for n in names if n in sys.modules)))\n"
)


def test_the_one_shot_imports_leave_docker_cron_and_the_content_scanner_unloaded() -> None:
    # W48-g-3: the Docker sweep's liveness answers, the in-process host's cron references and the
    # project-skill scanner import their heavy modules when first used.
    done = subprocess.run(
        [sys.executable, "-c", _LAZY], capture_output=True, text=True, timeout=120, check=True
    )
    assert json.loads(done.stdout.strip().splitlines()[-1]) == []
