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

/**
 * List filters for a tab. Picked sources (one or several, from the source menu) replace the tab's
 * own filter; a single string is accepted for older callers.
 */
export function filtersFor(tab: SessionTab, picked: string | readonly string[]): SessionListQuery {
  const list = typeof picked === "string" ? (picked ? [picked] : []) : picked;
  if (list.length === 1) return { source: list[0] as string };
  if (list.length > 1) return { sources: list };
  if (tab === "chats") return { excludeSources: AUTOMATION_SOURCES };
  if (tab === "automation") return { sources: AUTOMATION_SOURCES };
  return {};
}

/** True when a session from `source` is listed under `tab`. */
export function inTab(tab: SessionTab, source: string | null): boolean {
  if (tab === "all") return true;
  return tab === "automation" ? isAutomation(source) : !isAutomation(source);
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

/** Sources that are chat apps rather than a terminal or a schedule. */
const TERMINAL_SOURCES = ["cli", "tui", "desktop", "acp"] as const;

export type SourceKind = "terminal" | "web" | "automation" | "messaging" | "unknown";

/** What kind of place a session came from, for its icon. */
export function sourceKind(source: string | null): SourceKind {
  if (!source) return "unknown";
  if ((TERMINAL_SOURCES as readonly string[]).includes(source)) return "terminal";
  if (source === "web") return "web";
  if (isAutomation(source)) return "automation";
  return "messaging";
}

/** Names that do not follow the general rule below. */
const LABELS: Readonly<Record<string, string>> = {
  api_server: "API server",
  acp: "ACP",
  sms: "SMS",
  whatsapp: "WhatsApp",
};

/** Display name for a source: short ids are shouted ("CLI"), names are capitalised ("Telegram"). */
export function sourceLabel(source: string | null): string {
  if (!source) return "local";
  const fixed = LABELS[source];
  if (fixed) return fixed;
  if (source.length <= 3) return source.toUpperCase();
  return source.charAt(0).toUpperCase() + source.slice(1).replaceAll("_", " ");
}

/** The title of the source menu for a tab. */
export function sourceMenuTitle(tab: SessionTab): string {
  if (tab === "chats") return "Chat sources";
  if (tab === "automation") return "Automation sources";
  return "Sources";
}

/** The source button's label: the tab's "any" text, the one source, or "N sources". */
export function sourceButtonLabel(tab: SessionTab, picked: readonly string[]): string {
  if (picked.length === 0) return anySourceLabel(tab);
  if (picked.length === 1) return sourceLabel(picked[0] ?? null);
  return `${picked.length} sources`;
}

/** The placeholder of the source picker for a tab. */
export function anySourceLabel(tab: SessionTab): string {
  if (tab === "chats") return "Any chat source";
  if (tab === "automation") return "Any automation source";
  return "Any source";
}
