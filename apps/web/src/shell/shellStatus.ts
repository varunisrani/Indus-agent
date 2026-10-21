// The shell's live data (P18-25): the public status probe and the daemon's process, refreshed every
// 5 s while the tab is visible, shared by the status strip and the pressure banner (one query each,
// keyed by the active profile, so both read the same answer).

import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { api } from "../lib/api.ts";
import type { DaemonInfo, Pressure } from "../lib/restTypes.ts";
import { useProfileScope } from "./ProfileScope.tsx";

export const STATUS_REFRESH_MS = 5_000;

export function useShellStatus() {
  const { active } = useProfileScope();
  return useQuery({
    queryKey: ["shell", "status", active],
    queryFn: () => api.status(),
    refetchInterval: STATUS_REFRESH_MS,
    staleTime: 0,
  });
}

export function useShellDaemon() {
  const { active } = useProfileScope();
  return useQuery({
    queryKey: ["shell", "daemon", active],
    queryFn: () => api.daemon.get(),
    refetchInterval: STATUS_REFRESH_MS,
    staleTime: 0,
  });
}

/**
 * Which daemon process is serving: its pid while it runs, else null. The status probe is public
 * and carries no boot id, so a new process (a new pid) stands for a new boot.
 */
export function bootOf(daemon: DaemonInfo | undefined): string | null {
  return daemon?.running && daemon.pid !== null ? `pid-${daemon.pid}` : null;
}

export interface BootWatch {
  /** The current boot, or null while no daemon process is known. */
  boot: string | null;
  /** True when this boot began while the previous one's last memory reading was critical. */
  oomSuspected: boolean;
}

/**
 * Follow daemon boots and guess an out-of-memory restart: the process changed and the last memory
 * reading taken under the previous one was critical. A guess only; the banner says "may".
 */
export function useBootWatch(daemon: DaemonInfo | undefined, memory: Pressure | undefined) {
  const boot = bootOf(daemon);
  const lastBoot = useRef<string | null>(null);
  const lastMemory = useRef<Pressure | undefined>(undefined);
  const [oomBoot, setOomBoot] = useState<string | null>(null);

  useEffect(() => {
    if (boot === null) return;
    if (lastBoot.current !== null && lastBoot.current !== boot) {
      if (lastMemory.current === "critical") setOomBoot(boot);
      lastMemory.current = undefined;
    }
    lastBoot.current = boot;
  }, [boot]);

  useEffect(() => {
    // Readings count only while a daemon process is known (they belong to that boot).
    if (
      boot !== null &&
      boot === lastBoot.current &&
      memory !== undefined &&
      memory !== "unknown"
    ) {
      lastMemory.current = memory;
    }
  }, [boot, memory]);

  return { boot, oomSuspected: boot !== null && oomBoot === boot } satisfies BootWatch;
}
