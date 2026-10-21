// The resource banner (P18-25): at most one notice about the machine, the most urgent first —
// disk critical, memory critical, a suspected out-of-memory restart, disk elevated, memory
// elevated. `ok` and `unknown` show nothing. A dismissal (kept for this tab's session) holds for
// that notice during one daemon boot only; a worse notice (elevated → critical) is a different one
// and shows; a domain that is back to `ok` forgets its dismissals so its next episode shows again.

import { useEffect, useState } from "react";
import type { Pressure, StatusResponse } from "../lib/restTypes.ts";
import { useBootWatch, useShellDaemon, useShellStatus } from "./shellStatus.ts";

export type Trigger =
  | "disk-critical"
  | "memory-critical"
  | "oom-restart"
  | "disk-elevated"
  | "memory-elevated";

/** Most urgent first. */
export const PRIORITY: readonly Trigger[] = [
  "disk-critical",
  "memory-critical",
  "oom-restart",
  "disk-elevated",
  "memory-elevated",
];

export const DISMISSED_KEY = "indus.pressure.dismissed";

export interface PressureInput {
  memory: Pressure | undefined;
  disk: Pressure | undefined;
  oomSuspected: boolean;
}

/** Every notice the readings call for, most urgent first. */
export function activeTriggers({ memory, disk, oomSuspected }: PressureInput): Trigger[] {
  const on = new Set<Trigger>();
  if (disk === "critical") on.add("disk-critical");
  if (disk === "elevated") on.add("disk-elevated");
  if (memory === "critical") on.add("memory-critical");
  if (memory === "elevated") on.add("memory-elevated");
  if (oomSuspected) on.add("oom-restart");
  return PRIORITY.filter((t) => on.has(t));
}

export function dismissalKey(trigger: Trigger, boot: string | null): string {
  return `${trigger}:${boot ?? "unknown"}`;
}

/** The one notice to show, or null. */
export function pickBanner(
  input: PressureInput,
  boot: string | null,
  dismissed: readonly string[],
): Trigger | null {
  return (
    activeTriggers(input).find((trigger) => !dismissed.includes(dismissalKey(trigger, boot))) ??
    null
  );
}

/** Drop the dismissals of a domain that is back to `ok` (an `unknown` reading proves nothing). */
export function forgetRecovered(
  dismissed: readonly string[],
  input: Pick<PressureInput, "memory" | "disk">,
): string[] {
  return dismissed.filter((key) => {
    if (input.memory === "ok" && key.startsWith("memory-")) return false;
    if (input.disk === "ok" && key.startsWith("disk-")) return false;
    return true;
  });
}

function readDismissed(): string[] {
  try {
    const raw = window.sessionStorage.getItem(DISMISSED_KEY);
    const parsed: unknown = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed.filter((v): v is string => typeof v === "string") : [];
  } catch {
    return [];
  }
}

function writeDismissed(keys: readonly string[]): void {
  try {
    window.sessionStorage.setItem(DISMISSED_KEY, JSON.stringify(keys.slice(-50)));
  } catch {
    // storage unavailable: dismissals last for this page view
  }
}

export function bannerText(trigger: Trigger, status: StatusResponse | undefined): string {
  const freeGb = status?.disk?.free_gb;
  const used = status?.disk?.used_percent;
  const available = status?.memory?.available_percent;
  switch (trigger) {
    case "disk-critical":
      return `The disk is almost full${freeGb !== undefined ? ` (${freeGb} GB free)` : ""}. Sessions and logs may fail to save; free some space now.`;
    case "memory-critical":
      return `Memory is almost exhausted${available !== undefined ? ` (${available} % free)` : ""}. The system may stop the daemon to recover memory.`;
    case "oom-restart":
      return "The daemon restarted while memory was almost exhausted; the system may have stopped it for lack of memory.";
    case "disk-elevated":
      return `Disk space is getting low${used !== undefined ? ` (${used} % used)` : ""}.`;
    case "memory-elevated":
      return `Memory is getting low${available !== undefined ? ` (${available} % free)` : ""}.`;
  }
}

export interface PressureBannerViewProps {
  status: StatusResponse | undefined;
  boot: string | null;
  oomSuspected: boolean;
}

/** The banner for given readings (the shell wires the live ones in `PressureBanner`). */
export function PressureBannerView({ status, boot, oomSuspected }: PressureBannerViewProps) {
  const [dismissed, setDismissed] = useState<string[]>(readDismissed);
  const memory = status?.memory?.pressure;
  const disk = status?.disk?.pressure;

  useEffect(() => {
    setDismissed((was) => {
      const next = forgetRecovered(was, { memory, disk });
      if (next.length === was.length) return was;
      writeDismissed(next);
      return next;
    });
  }, [memory, disk]);

  const input = { memory, disk, oomSuspected };
  const trigger = pickBanner(input, boot, dismissed);
  if (!trigger) return null;
  const critical = trigger.endsWith("critical") || trigger === "oom-restart";
  const dismiss = () => {
    setDismissed((was) => {
      const key = dismissalKey(trigger, boot);
      const next = was.includes(key) ? was : [...was, key];
      writeDismissed(next);
      return next;
    });
  };
  return (
    <div
      role={critical ? "alert" : "status"}
      data-testid="pressure-banner"
      data-trigger={trigger}
      className={`flex items-start gap-3 border-b bg-raised px-4 py-2 text-sm ${
        critical ? "border-danger text-danger" : "border-warn text-warn"
      }`}
    >
      <span className="flex-1">{bannerText(trigger, status)}</span>
      <button type="button" className="text-muted" aria-label="Dismiss" onClick={dismiss}>
        ×
      </button>
    </div>
  );
}

export function PressureBanner() {
  const status = useShellStatus();
  const daemon = useShellDaemon();
  const { boot, oomSuspected } = useBootWatch(daemon.data, status.data?.memory?.pressure);
  return <PressureBannerView status={status.data} boot={boot} oomSuspected={oomSuspected} />;
}
