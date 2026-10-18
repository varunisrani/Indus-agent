# pyright: strict
"""What file-changing tools do right before they change files (P11-16, P11-20).

- ``before_file_change``: ``write_file`` and ``patch`` ask the checkpoint trigger for the
  session's once-per-turn snapshot of its working folder, and check that the edited paths lie
  inside the session's worktree when it works in one.
- ``before_command``: ``terminal`` does the same for a command that changes files
  (``checkpoints.trigger.is_destructive``), only when checkpoints are on.
- ``with_notes``: adds the returned notes (``checkpoint_warning``, ``worktree_warning``) to a JSON
  tool result without replacing a key the tool set itself.

None of these ever stops the tool.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping
from typing import Any

from indus.backends import worktrees
from indus.backends.checkpoints.trigger import TRIGGER, is_destructive
from indus.foundation.scope import Scope
from indus.tools.builtin.file_ops import FileOps

__all__ = ["before_command", "before_file_change", "with_notes"]

log = logging.getLogger(__name__)


async def before_file_change(
    ops: FileOps,
    *,
    scope: Scope | None,
    session_id: str | None,
    turn_id: str | None,
    paths: Iterable[str] = (),
) -> dict[str, str]:
    notes: dict[str, str] = {}
    if scope is None:
        return notes
    warning = await TRIGGER.abefore_mutation(
        scope, session_id=session_id, turn_id=turn_id, workdir=ops.cwd, on_host=ops.host
    )
    if warning:
        notes["checkpoint_warning"] = warning
    if ops.host:
        try:
            note = worktrees.divergence_note(scope, ops.cwd, list(paths))
        except Exception:
            log.debug("worktree check failed", exc_info=True)
            note = None
        if note:
            notes["worktree_warning"] = note
    return notes


async def before_command(
    scope: Scope,
    *,
    session_id: str | None,
    turn_id: str | None,
    command: str,
    cwd: str,
    on_host: bool,
) -> dict[str, str]:
    if not TRIGGER.enabled(scope, session_id) or not is_destructive(command):
        return {}
    warning = await TRIGGER.abefore_mutation(
        scope,
        session_id=session_id,
        turn_id=turn_id,
        workdir=cwd,
        on_host=on_host,
        reason="before a terminal command",
    )
    return {"checkpoint_warning": warning} if warning else {}


def with_notes(result: str, notes: Mapping[str, str]) -> str:
    if not notes:
        return result
    try:
        body: Any = json.loads(result)
    except ValueError:
        return result
    if not isinstance(body, dict):
        return result
    merged: dict[str, Any] = dict(body)  # pyright: ignore[reportUnknownArgumentType]
    for key, value in notes.items():
        merged.setdefault(key, value)
    return json.dumps(merged, ensure_ascii=False)
