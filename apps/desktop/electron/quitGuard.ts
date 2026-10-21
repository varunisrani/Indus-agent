// Quit semantics by ownership (P20-13, R08.24) and the GPU crash relaunch (D-16.81).
//
// Windows report the turns they see running (`indus:work:active`). Local and remote daemons
// outlive the app, so quitting with running turns only informs — the agent keeps working and the
// person can reconnect later — and never stops anything. Work the app itself owns (in 1.0: an
// install in progress) would be lost, so quitting then asks to confirm. Up to 4 items are listed.

export const MAX_LISTED = 4;

export interface WorkItem {
  id: string;
  label: string;
}

export interface QuitPrompt {
  message: string;
  detail: string;
  /** [keep the app open, quit] */
  buttons: [string, string];
  /** True when quitting loses nothing (the backend keeps working). */
  informational: boolean;
}

export class QuitGuard {
  private readonly reports = new Map<number, WorkItem[]>();

  /** A window's current list (replaces its previous report). */
  report(senderId: number, items: readonly WorkItem[]): void {
    if (items.length === 0) this.reports.delete(senderId);
    else
      this.reports.set(
        senderId,
        items.map((item) => ({ id: item.id, label: item.label.trim() })),
      );
  }

  /** A closed window stops counting. */
  forget(senderId: number): void {
    this.reports.delete(senderId);
  }

  /** Every window's items, each id once (windows can show the same session). */
  work(): WorkItem[] {
    const seen = new Map<string, WorkItem>();
    for (const items of this.reports.values())
      for (const item of items) if (!seen.has(item.id)) seen.set(item.id, item);
    return [...seen.values()];
  }
}

function listed(items: readonly WorkItem[]): string {
  const shown = items
    .slice(0, MAX_LISTED)
    .map((item) => `• ${item.label === "" ? "Untitled chat" : item.label}`);
  const rest = items.length - shown.length;
  if (rest > 0) shown.push(rest === 1 ? "• 1 more" : `• ${rest} more`);
  return shown.join("\n");
}

/**
 * What to ask before quitting, or null to quit at once. `attachedWork` runs on a daemon that
 * outlives the app; `ownedWork` dies with it.
 */
export function quitPrompt(
  attachedWork: readonly WorkItem[],
  ownedWork: readonly WorkItem[],
): QuitPrompt | null {
  if (ownedWork.length > 0) {
    return {
      message:
        ownedWork.length === 1
          ? "Indus is still busy with 1 task."
          : `Indus is still busy with ${ownedWork.length} tasks.`,
      detail: `${listed(ownedWork)}\n\nQuitting now stops this work and what it has not finished is lost.`,
      buttons: ["Keep running", "Quit anyway"],
      informational: false,
    };
  }
  if (attachedWork.length > 0) {
    return {
      message:
        attachedWork.length === 1
          ? "The agent is still working on 1 chat."
          : `The agent is still working on ${attachedWork.length} chats.`,
      detail: `${listed(attachedWork)}\n\nThe agent keeps working after you quit. Open Indus again later to see the results.`,
      buttons: ["Cancel", "Quit"],
      informational: true,
    };
  }
  return null;
}

// -- GPU child crash -----------------------------------------------------------------------------

export interface ChildGoneDetails {
  type: string;
  reason: string;
  exitCode: number;
}

/** Exit codes of a GPU process killed by a known driver/sandbox fault. */
const GPU_CRASH_CODES = new Set([
  -1073740791, // 0xC0000409 stack buffer overrun (Windows)
  -1073741819, // 0xC0000005 access violation (Windows)
  139, // SIGSEGV
  134, // SIGABRT
]);

/**
 * One bounded relaunch with the GPU disabled when the GPU child dies with a known signature, once
 * per app version (`marker` is what the last relaunch recorded). The renderer sandbox is never
 * turned off (D-16.81).
 */
export function gpuCrashDecision(
  details: ChildGoneDetails,
  appVersion: string,
  marker: { version: string } | null,
  argv: readonly string[],
): "relaunch-without-gpu" | "ignore" {
  if (details.type !== "GPU") return "ignore";
  if (
    details.reason !== "crashed" &&
    details.reason !== "abnormal-exit" &&
    details.reason !== "launch-failed"
  ) {
    return "ignore";
  }
  if (details.reason !== "launch-failed" && !GPU_CRASH_CODES.has(details.exitCode)) return "ignore";
  if (argv.includes("--disable-gpu")) return "ignore";
  if (marker !== null && marker.version === appVersion) return "ignore";
  return "relaunch-without-gpu";
}

/** The argv for the relaunch: the person's own switches plus `--disable-gpu` (nothing about the sandbox). */
export function relaunchArgs(argv: readonly string[]): string[] {
  return [...argv, "--disable-gpu"];
}
