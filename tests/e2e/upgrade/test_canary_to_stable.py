"""P25-22: a canary source install updates to the next stable with its data in place.

The source-install half of the canary dress rehearsal (skill reference 08 §8), with no network
and nothing published:

1. two releases are built locally from this checkout's own source tree: a canary
   ``X.Y.Z-canary.<stamp>`` and then the stable ``X.Y.Z`` it leads to (names from
   ``scripts/release/versioning.py``), tagged in a temp bare repository that plays ``origin``;
2. the canary is installed with the real ``install.sh`` stages (``INDUS_INSTALL_REPO_URL`` points
   them at the bare repository; the dependencies step runs offline, see ``world.py``);
3. the canary's own CLI gives the install some data: a session with one turn against the scripted
   model server, a credential in the secret store, a second profile, and the launcher;
4. the stable is published and the canary's ``indus update`` runs through the launcher.

The update must be offered on the saved ``canary`` channel without switching it (a canary install
reads stable tags too), report the version it moves from and to, leave a clean checkout on the
stable tag, and the new code must read every piece of data back.

Release lane only (``release`` marker, ``INDUS_RELEASE_LANE=1``; release.yml's ``ci`` job sets
it): two real environments are built, which is too slow for the PR gate.
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from typing import Any

from scripts.release.versioning import next_canary, next_stable
from tests.e2e.upgrade.world import RELEASE_LANE_MARKS, TURN_TIMEOUT_S, ModelServer, Rehearsal
from tests.fixtures.update.make_repo import git

CANARY_AT = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
SESSION_ID = re.compile(r"^(\d{8}_\d{6}_[0-9a-z]+)\s", re.MULTILINE)
EXPORTED = re.compile(r"Exported 1 session\(s\), (\d+) message\(s\)")
# The secret store (its file fallback: the lane has no keychain) and the credential pools: the
# update must leave them byte for byte.
KEPT_FILES = ("secrets.json", "secrets.index.json", "auth.json")

pytestmark = RELEASE_LANE_MARKS


def plan_of(world: Rehearsal) -> dict[str, Any]:
    done = world.indus("update", "--plan")
    assert done.returncode == 0, world.story()
    return json.loads(done.stdout)


def session_ids(world: Rehearsal) -> list[str]:
    """The one-shot sessions ``indus sessions list`` shows (its first column), newest first."""
    done = world.indus("sessions", "list", "--source", "oneshot", "--limit", "50")
    assert done.returncode == 0, world.story()
    return SESSION_ID.findall(done.stdout)


def message_count(world: Rehearsal, session: str) -> int:
    """How many messages the store holds for ``session`` (read through ``sessions export``)."""
    out = world.work / f"{session}.jsonl"
    out.unlink(missing_ok=True)
    done = world.indus(
        "sessions", "export", "--session-id", session, "--format", "jsonl", "--yes", str(out)
    )
    assert done.returncode == 0, world.story()
    found = EXPORTED.search(done.stdout)
    assert found is not None, world.story()
    return int(found.group(1))


def test_a_canary_install_updates_to_the_next_stable_with_its_data(
    rehearsal: Rehearsal, model_server: ModelServer
) -> None:
    world = rehearsal
    canary = next_canary(None, CANARY_AT)
    stable = next_stable(None)
    assert canary < stable and canary.is_canary and not stable.is_canary
    world.release(str(canary))

    # -- the previous canary, installed and in use ---------------------------------------------
    iid = world.install("canary")
    launcher_text = world.launcher.read_text(encoding="utf-8")
    assert str(world.checkout / ".venv" / "bin" / "indus") in launcher_text
    for key, value in (
        ("model.provider", "custom"),
        ("model.base_url", model_server.base_url),
        ("model.default", "echo"),
    ):
        world.ok([str(world.launcher), "config", "set", key, value])
    first = world.indus("--local", "-z", "remember the blue kite", timeout=TURN_TIMEOUT_S)
    assert first.returncode == 0 and "You said: remember the blue kite" in first.stdout, (
        world.story()
    )
    [session] = session_ids(world)
    assert message_count(world, session) == 2
    key = "fake" + "-" + "rehearsal" + "-" + "credential" + "-0123456789"
    added = world.indus("auth", "add", "openrouter", "--api-key", key, "--label", "rehearsal")
    assert added.returncode == 0, world.story()
    assert world.indus("profile", "create", "work").returncode == 0, world.story()
    before = plan_of(world)
    assert before["channel"] == "canary" and before["channel_source"] == "saved"
    assert before["action"] == "up_to_date"
    assert before["from"]["version"] == str(canary)
    kept = {name: (world.home / name).read_bytes() for name in KEPT_FILES}

    # -- the stable is published -----------------------------------------------------------------
    stable_sha = world.release(str(stable))
    check = world.indus("update", "--check")
    assert check.returncode == 10, world.story()
    assert f"Update available on canary: {stable}" in check.stdout
    plan = plan_of(world)
    assert plan["channel"] == "canary" and plan["channel_source"] == "saved"
    assert plan["action"] == "update"
    assert plan["to"] == {"commit": stable_sha, "version": str(stable), "tag": f"v{stable}"}

    # -- the update, through the launcher --------------------------------------------------------
    updated = world.indus("update", "-y", "--no-gateway-restart")
    assert updated.returncode == 0, world.story()
    assert f"Updated to {stable}" in updated.stdout, world.story()
    assert git(world.checkout, "rev-parse", "HEAD", home=world.upstream.home) == stable_sha
    assert git(world.checkout, "status", "--porcelain", home=world.upstream.home) == ""
    assert (world.checkout / ".venv-prev").is_dir() and not (world.checkout / ".venv-next").exists()
    assert (
        (world.checkout / "RELEASE.txt")
        .read_text(encoding="utf-8")
        .startswith(f"Indus Agent {stable} ")
    )
    receipts = sorted((world.home / "logs" / "update_receipts").glob("*.json"))
    receipt = json.loads(receipts[-1].read_text(encoding="utf-8"))
    # The rehearsal's releases are plain annotated tags: no release record, so no manifest digest
    # and no published dashboard pages. A real release tag carries `manifest_sha256`
    # (`scripts/release/publish.py`), so the page refresh (wave 47, `web_pages`) is the one
    # follow-up owed here, and only for that reason.
    assert receipt["outcome"] == "ok_with_followups" and receipt["channel"] == "canary", receipt
    assert receipt["to_version"] == str(stable), receipt
    [owed] = receipt["owed"]
    assert owed["followup"] == "web_pages", receipt
    assert f"the v{stable} tag records no release manifest digest" in owed["detail"], receipt
    phases = {row["phase"]: row["outcome"] for row in receipt["phases"]}
    assert phases["snapshot"] == "ok" and phases["swap"] == "ok", receipt
    assert phases["web_pages"] == "owed", receipt
    assert not (world.home / ".indus-update-in-progress").exists()

    # -- after: same channel, version reported, data intact -------------------------------------
    again = world.indus("update", "--check")
    assert again.returncode == 0 and f"already on {stable}" in again.stdout, world.story()
    after = plan_of(world)
    assert after["channel"] == "canary" and after["channel_source"] == "saved"
    assert after["from"]["version"] == str(stable)
    assert {name: (world.home / name).read_bytes() for name in KEPT_FILES} == kept
    channel = world.indus("config", "get", f"updates.installs.{iid}.channel")
    assert channel.returncode == 0 and channel.stdout.strip() == "canary", world.story()
    version = world.indus("--version")
    assert version.returncode == 0, world.story()
    assert "Install method: git" in version.stdout
    assert f"Install directory: {world.checkout}" in version.stdout
    assert world.launcher.read_text(encoding="utf-8") == launcher_text
    assert os.access(world.launcher, os.X_OK)
    assert session_ids(world) == [session]
    resumed = world.indus(
        "--local", "-r", session, "-z", "what did I ask you", timeout=TURN_TIMEOUT_S
    )
    assert resumed.returncode == 0 and "You said: what did I ask you" in resumed.stdout, (
        world.story()
    )
    assert session_ids(world) == [session]
    assert message_count(world, session) == 4
    pools = world.indus("auth", "list")
    assert pools.returncode == 0 and "rehearsal" in pools.stdout, world.story()
    assert key not in pools.stdout
    profiles = world.indus("profile", "list")
    assert profiles.returncode == 0 and "work" in profiles.stdout, world.story()
    assert world.gateway_pid() is None, "a --local turn left a gateway daemon running"
