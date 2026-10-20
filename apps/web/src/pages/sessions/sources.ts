// Session list tabs and the plain-language text for fenced-delete refusals (P18-19).

import { ApiError, type SessionListQuery } from "../../lib/api.ts";

/** Sources counted as automation: scheduled jobs, tool-spawned runs, one-shots, API and ACP. */
export const AUTOMATION_SOURCES = ["cron", "tool", "oneshot", "api_server", "acp"] as const;

export type SessionTab = "chats" | "automation" | "all";

export const SESSION_TABS: ReadonlyArray<{ value: SessionTab; label: string }> = [
  { value: "chats", label: "Chats" },
  { value: "automation", label: "Automation" },
  { value: "all", label: "All" },
];

export function isAutomation(source: string | null): boolean {
  return source !== null && (AUTOMATION_SOURCES as readonly string[]).includes(source);
}

/** List filters for a tab; an exact source, when picked, replaces the tab's filter. */
export function filtersFor(tab: SessionTab, source: string): SessionListQuery {
  if (source) return { source };
  if (tab === "chats") return { excludeSources: AUTOMATION_SOURCES };
  if (tab === "automation") return { sources: AUTOMATION_SOURCES };
  return {};
}

export const CONFLICT_MESSAGES: Readonly<Record<string, string>> = {
  session_busy:
    "This session has a turn running or queued. Wait for it to finish (or stop it in Chat), then delete it.",
  unknown_execution:
    "The outcome of this session's last turn is unknown. Open it in Chat and resolve that turn before deleting.",
  mutation_identity_required:
    "The delete request was missing part of its safety check. Reload the page and try again.",
  client_upgrade_required:
    "This page is older than the server. Reload the page to get the current dashboard, then try again.",
  daemon_running:
    "Bulk clean-up only runs while the daemon is stopped. Stop it on the System page and try again.",
};

/** The sentence to show for a failed session write. */
export function conflictMessage(error: unknown): string {
  if (error instanceof ApiError && error.status === 409 && error.code) {
    return CONFLICT_MESSAGES[error.code] ?? error.message;
  }
  return error instanceof Error ? error.message : "The request failed.";
}
