"""What ``indus chat`` hands to the classic REPL (``indus.cli.repl.app.run_repl``).

The REPL gets a started ``InProcessHost`` with one attached session and never touches the store or
the tools itself. ``history`` is the stored transcript of a resumed session (for the recap);
``seed`` is a ``-q`` text to submit literally as the first turn (no slash or ``!`` handling).
``store`` is the chat's store handle (slash commands read and write session rows through it).
The entry (``indus.cli.launch.run_launch``) builds the slash context and calls the REPL; it returns
the process exit code and ``chat`` stops the host afterwards.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from indus.cli.session_select import LaunchPolicy
    from indus.core.transcript import TranscriptRow
    from indus.foundation.scope import Scope
    from indus.service.host_inprocess import InProcessHost
    from indus.state.registry import StoreHandle

__all__ = ["REPL_ENTRY", "ReplEntry", "ReplLaunch"]

REPL_ENTRY = "indus.cli.launch:run_launch"
"""What starts the classic REPL (P06-04) for a launch, with slash commands wired (P06-09)."""


@dataclass
class ReplLaunch:
    """One interactive chat, ready to run."""

    scope: Scope
    host: InProcessHost
    session_id: str
    policy: LaunchPolicy
    model: str
    provider: str
    cwd: Path
    resumed: bool = False
    title: str | None = None
    history: Sequence[TranscriptRow] = field(default_factory=list["TranscriptRow"])
    seed: str | None = None
    notices: Sequence[str] = ()
    store: StoreHandle | None = None
    no_restore_cwd: bool = False
    """``--no-restore-cwd`` / ``--in``: a ``/resume`` in the REPL keeps the folder too."""
    voice: bool = False
    """``--voice``: voice mode is turned on once the prompt is up (P10-19)."""


class ReplEntry(Protocol):
    def __call__(self, launch: ReplLaunch, /) -> int: ...
