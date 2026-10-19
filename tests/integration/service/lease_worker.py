"""One process of the turn-lease contention test (P17-03); the parent is
``test_turn_lease_multiprocess.py``.

Run as ``python lease_worker.py <mode> <home> <session>``. The worker opens the store in ``home``
(a temp ``INDUS_HOME``) with the real clock and short lease timings, then:

- ``hold``: takes the lease (renewing it), prints ``held <generation> <holder>``, waits for a line
  on stdin, releases, prints ``released``.
- ``wait``: takes the lease, printing ``waiting`` once while another holder has it, then prints
  ``held <generation> <holder>`` and releases at once.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from indus.foundation.clock import SystemClock
from indus.foundation.scope import EnvView, make_scope
from indus.service.turn_lease import HeldLease, LeaseSettings, TurnLeases
from indus.state import registry

SETTINGS = LeaseSettings(ttl=3.0, refresh_every=0.5, write_patience=1.0, poll=0.05, wait=60.0)


def say(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


async def main(mode: str, home: Path, session_id: str) -> int:
    env = EnvView.from_process().with_overlay({"INDUS_HOME": str(home)})
    store = registry.open_unshared(make_scope(home, env=env, clock=SystemClock()))
    try:
        leases = TurnLeases(store, settings=SETTINGS)
        told: list[float] = []

        def waiting(elapsed: float) -> None:
            if not told:
                say("waiting")
            told.append(elapsed)

        held = await leases.acquire(session_id, on_wait=waiting)
        if not isinstance(held, HeldLease):
            say(f"failed {held.reason}")
            return 1
        say(f"held {held.generation} {held.holder_id}")
        if mode == "hold":
            held.start(on_lost=lambda reason: say(f"lost {reason}"))
            await asyncio.get_running_loop().run_in_executor(None, sys.stdin.readline)
        await held.release()
        say("released")
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1], Path(sys.argv[2]), sys.argv[3])))
