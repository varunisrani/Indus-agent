// What an app window tells main and the person about running work (P20-36):
//
// - `WorkTracker` follows `turn.started` / `turn.done` and gives the list of running turns this
//   window sees; the window reports it (`work.report`) — the quit guard and keep awake read it.
// - `turnNotice` / `requestNotice` decide the OS notification for a finished turn or a question
//   waiting for the person, only while the window is not focused. Tags carry the session so a
//   click can open it (`tagSession`).

import type { MessageKey, Translate } from "../i18n/index.tsx";

export interface WorkItem {
  id: string;
  label: string;
}

interface TurnEvent {
  type: string;
  session_id?: string;
  turn_id?: string;
}

export class WorkTracker {
  private readonly running = new Map<string, string>();

  /** Feed an event; true when the running list changed. */
  onEvent(event: TurnEvent): boolean {
    const sessionId = event.session_id;
    if (typeof sessionId !== "string") return false;
    if (event.type === "turn.started" && typeof event.turn_id === "string") {
      const had = this.running.get(sessionId);
      this.running.set(sessionId, event.turn_id);
      return had === undefined;
    }
    if (event.type === "turn.done") {
      const had = this.running.get(sessionId);
      if (had === undefined) return false;
      if (typeof event.turn_id === "string" && event.turn_id !== had) return false;
      this.running.delete(sessionId);
      return true;
    }
    return false;
  }

  /** Forget everything (the connection went away for good). */
  clear(): boolean {
    const had = this.running.size > 0;
    this.running.clear();
    return had;
  }

  items(): WorkItem[] {
    // No title is known here; the quit guard shows "Untitled chat" for an empty label.
    return [...this.running.keys()].map((id) => ({ id, label: "" }));
  }
}

export interface NoticeRequest {
  title: string;
  body: string;
  tag: string;
  actions: Array<{ id: string; label: string }>;
}

export function tagSession(tag: string | null): string | null {
  if (tag === null) return null;
  const match = /^(?:turn|ask):(.+)$/.exec(tag);
  return match === null ? null : (match[1] ?? null);
}

interface DoneEvent {
  type: string;
  session_id?: string;
  reason?: string;
  failure?: { reason?: string } | null;
}

/** The notification for a finished turn, or null (still running, stopped by the person, focused). */
export function turnNotice(event: DoneEvent, focused: boolean, t: Translate): NoticeRequest | null {
  if (focused || event.type !== "turn.done" || typeof event.session_id !== "string") return null;
  if (event.reason === "interrupted_by_user") return null;
  const actions = [{ id: "open", label: t("notify.open") }];
  const tag = `turn:${event.session_id}`;
  if (event.reason === "completed" && (event.failure ?? null) === null) {
    return { title: t("notify.turnDone.title"), body: t("notify.turnDone.body"), tag, actions };
  }
  const reason = (event.failure?.reason ?? "").trim() || (event.reason ?? "").replace(/_/g, " ");
  return { title: t("notify.turnFailed.title"), body: reason, tag, actions };
}

/** The notification switches (Settings › Notifications › Desktop alerts, W59-K1-1). */
export interface NoticeKinds {
  readonly turnComplete?: boolean;
  readonly turnFailed?: boolean;
  readonly approvals?: boolean;
  readonly input?: boolean;
  readonly plugins?: boolean;
}

/** The switch that covers a finished turn: a failure has its own (stopped turns raise none). */
export function turnKind(event: DoneEvent): "turnComplete" | "turnFailed" {
  return event.reason === "completed" && (event.failure ?? null) === null
    ? "turnComplete"
    : "turnFailed";
}

/** The switch that covers a question: approvals have their own, the rest are "input needed". */
export function requestKind(kind: string): "approvals" | "input" {
  return kind === "approval" ? "approvals" : "input";
}

/** A kind is on unless the person turned it off (before the prefs arrive, everything is on). */
export function noticeAllowed(kinds: NoticeKinds | null, kind: keyof NoticeKinds): boolean {
  return kinds?.[kind] !== false;
}

/** The notification for a desktop plugin's message (`ctx.toast`), only while not focused. */
export function pluginNotice(text: string, focused: boolean, t: Translate): NoticeRequest | null {
  const body = text.trim();
  if (focused || body === "") return null;
  return { title: t("notify.plugin.title"), body: body.slice(0, 500), tag: "plugin", actions: [] };
}

/** The notification for a question the backend asks (approval, clarify, sudo, secret). */
export function requestNotice(
  kind: string,
  sessionId: string,
  focused: boolean,
  t: Translate,
): NoticeRequest | null {
  if (focused) return null;
  const body: MessageKey = kind === "approval" ? "notify.ask.approval" : "notify.ask.other";
  return {
    title: t("notify.ask.title"),
    body: t(body),
    tag: `ask:${sessionId}`,
    actions: [{ id: "open", label: t("notify.open") }],
  };
}
