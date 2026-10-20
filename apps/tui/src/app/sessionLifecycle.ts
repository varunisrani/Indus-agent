// Opening sessions (P12-27, research 05 §3.6 and §3.19): what happens on each ready frame, and the
// create / resume / switch / close paths the slash commands and the session switcher share.
//
// Bootstrap order on every ready: a recovery target left by a crash is reopened first (never with
// the startup prompt); else the launcher's resume id; else the most recent session when the
// `display.tui_auto_resume_recent` setting is on; else a new session. The startup prompt goes out
// once per process, literally. A create whose answer was lost is repeated with the same
// idempotency key so the bridge hands back the session it already made (R05.21).

import { randomUUID } from "node:crypto";
import { writeFileSync } from "node:fs";
import {
  GatewayRpcError,
  NotConnected,
  RpcTimeout,
  TransportClosed,
  TransportReplaced,
} from "@indus/client";
import type {
  HistoryMessage,
  SessionCreateResult,
  SessionListResult,
  SessionMostRecentResult,
  SessionResumeResult,
  SessionRow,
} from "@indus/protocol";
import type { StoreApi } from "zustand/vanilla";
import { focusSession } from "../state/destination.ts";
import {
  type NewMessage,
  type TranscriptState,
  transcriptStore,
} from "../state/transcriptStore.ts";
import { STATUS_READY, type UiState, uiStore } from "../state/uiStore.ts";
import {
  declaredOnly,
  ENV,
  type Env,
  launchOptions,
  type StartupPrompt,
  startupPrompt,
  startupResumeId,
  toolProgressMode,
} from "./launchOptions.ts";

export const STATUS_RECOVERING = "recovering session…";
export const STATUS_RESUMING = "resuming…";
export const STATUS_RESUMING_RECENT = "resuming your latest session…";
export const STATUS_STARTING_SESSION = "starting session…";
export const STATUS_SWITCHING = "switching session…";
export const BUSY_SWITCH_LINE =
  "A reply is still running: stop the turn first (Esc Esc or Ctrl+C).";
export const AUTO_RESUME_KEY = "display.tui_auto_resume_recent";
/** How many stored sessions the switcher asks for. */
export const STORED_LIMIT = 50;

/** The calls this module makes; `IndusClient` + `rpcFromClient` provide them in the app. */
export interface SessionPort {
  call(method: string, params: object): Promise<unknown>;
  /** `session.resume` that also hands still-open server requests to the cards again. */
  resume(params: { session_id: string }): Promise<SessionResumeResult>;
}

export type OpenReason = "recover" | "startup" | "recent" | "switch" | "create";

export interface LifecycleDeps {
  port: SessionPort;
  env: Env;
  ui?: StoreApi<UiState>;
  transcript?: StoreApi<TranscriptState>;
  /** Drop the live turn without writing anything (switch, reopen). */
  resetTurn(): void;
  system(text: string): void;
  userLine(text: string): void;
  /** The newest `session.info` seen for a session (events of other sessions are filtered). */
  infoFor?(sessionId: string): Record<string, unknown> | null;
  /** A session became the open one. */
  onOpened?(sessionId: string, reason: OpenReason): void;
  newKey?(): string;
  writeFile?(path: string, text: string): void;
}

export type OpenResult =
  | { readonly ok: true; readonly sessionId: string }
  | { readonly ok: false; readonly link: boolean; readonly message: string };

/** A session this process has attached (shown as "live" in the switcher). */
export interface LiveSession {
  readonly id: string;
  title: string | null;
  model: string | null;
}

/** Errors that mean "the child went away", not "the server said no". */
export function isLinkError(error: unknown): boolean {
  return (
    error instanceof TransportClosed ||
    error instanceof TransportReplaced ||
    error instanceof NotConnected ||
    error instanceof RpcTimeout
  );
}

function messageOf(error: unknown): string {
  return error instanceof Error && error.message ? error.message : String(error);
}

/** The stored conversation as transcript rows. */
export function historyRows(messages: readonly HistoryMessage[]): NewMessage[] {
  return messages.map((m) =>
    m.role === "tool"
      ? { role: "tool", text: m.name ? `${m.name}: ${m.text}` : m.text }
      : { role: m.role, text: m.text },
  );
}

/** Sources the switcher never lists (delegated runs). */
const HIDDEN_SOURCES: ReadonlySet<string> = new Set(["subagent", "delegate", "delegation"]);

export function visibleStored(rows: readonly SessionRow[]): SessionRow[] {
  return rows.filter((row) => !HIDDEN_SOURCES.has((row.source ?? "").toLowerCase()));
}

export class SessionLifecycle {
  private readonly deps: LifecycleDeps;
  private readonly ui: StoreApi<UiState>;
  private readonly transcript: StoreApi<TranscriptState>;
  private readonly live = new Map<string, LiveSession>();
  /** The create waiting for an answer: same options → same key. */
  private pendingCreate: { signature: string; key: string } | null = null;
  private flight = 0;
  private startupDone = false;
  private startupResumeDone = false;
  private droppedSaid = false;
  /** Lines for the transcript of the next session opened (the open resets the transcript). */
  private readonly carried: string[] = [];

  constructor(deps: LifecycleDeps) {
    this.deps = deps;
    this.ui = deps.ui ?? uiStore;
    this.transcript = deps.transcript ?? transcriptStore;
  }

  /** Sessions attached in this process, newest first. */
  liveSessions(): LiveSession[] {
    return [...this.live.values()].reverse();
  }

  /** The key a retried create would reuse (tests, diagnostics). */
  get pendingKey(): string | null {
    return this.pendingCreate?.key ?? null;
  }

  private status(text: string): void {
    this.ui.getState().patch({ statusText: text });
  }

  /** Open a session after a ready frame. `target` is the recovery target, if any. */
  async bootstrap(target: string | null): Promise<OpenResult> {
    if (target !== null) {
      this.status(STATUS_RECOVERING);
      const result = await this.open(target, "recover");
      if (!result.ok && !result.link) {
        this.deps.system(`error: could not reopen session ${target}: ${result.message}`);
        this.status("could not reopen the session");
      }
      return result;
    }

    const resumeId = this.startupResumeDone ? null : startupResumeId(this.deps.env);
    if (resumeId !== null) {
      this.status(STATUS_RESUMING);
      const result = await this.open(resumeId, "startup");
      if (result.ok || !result.link) this.startupResumeDone = true;
      if (result.ok) await this.sendStartupPrompt();
      if (result.ok || result.link) return result;
      // A stale or mistyped id: say so and start fresh rather than leave no session at all.
      this.carried.push(`error: could not resume ${resumeId}: ${result.message}`);
    } else {
      const recent = await this.mostRecent();
      if (recent !== null) {
        this.status(STATUS_RESUMING_RECENT);
        const result = await this.open(recent, "recent");
        if (result.ok) await this.sendStartupPrompt();
        if (result.ok || result.link) return result;
      }
    }

    this.status(STATUS_STARTING_SESSION);
    const result = await this.create();
    if (result.ok) await this.sendStartupPrompt();
    return result;
  }

  /** The most recent session when auto-resume is on; null when off, absent or unreadable. */
  private async mostRecent(): Promise<string | null> {
    try {
      const setting = (await this.deps.port.call("config.get", { key: AUTO_RESUME_KEY })) as {
        value?: unknown;
      } | null;
      if (setting?.value !== true) return null;
      const found = (await this.deps.port.call(
        "session.most_recent",
        {},
      )) as SessionMostRecentResult | null;
      return found?.session?.session_id ?? null;
    } catch {
      return null;
    }
  }

  /** Resume `id` and make it the open session. */
  async open(id: string, reason: OpenReason): Promise<OpenResult> {
    this.flight += 1;
    const mine = this.flight;
    let result: SessionResumeResult;
    try {
      result = await this.deps.port.resume({ session_id: id });
    } catch (error) {
      if (mine === this.flight) this.status(STATUS_READY);
      return { ok: false, link: isLinkError(error), message: messageOf(error) };
    }
    if (mine !== this.flight) return { ok: false, link: false, message: "superseded" };
    this.adopt(result.session_id, reason, {
      title: result.title ?? null,
      messages: result.messages ?? [],
    });
    return { ok: true, sessionId: result.session_id };
  }

  /** Create a session with the launch options (and `title`). */
  async create(title?: string): Promise<OpenResult> {
    this.flight += 1;
    const mine = this.flight;
    const options = launchOptions(this.deps.env);
    if (title?.trim()) options.title = title.trim();
    const { params, dropped } = declaredOnly(options);
    if (dropped.length > 0 && !this.droppedSaid) {
      this.droppedSaid = true;
      this.carried.push(
        `note: this agent does not take these launch options yet, so they were left out: ${dropped.join(", ")}`,
      );
    }
    const signature = JSON.stringify(params);
    if (this.pendingCreate?.signature !== signature) {
      this.pendingCreate = { signature, key: (this.deps.newKey ?? randomUUID)() };
    }
    const sent = { ...params, idempotency_key: this.pendingCreate.key };
    let created: SessionCreateResult;
    try {
      created = await this.createOnce(sent);
    } catch (error) {
      const link = isLinkError(error);
      // A lost answer keeps the key for the next try; a refusal starts over next time.
      if (!link) this.pendingCreate = null;
      if (mine === this.flight) {
        if (!link) this.deps.system(`error: could not start a session: ${messageOf(error)}`);
        this.status(link ? "agent not answering" : "no session");
      }
      return { ok: false, link, message: messageOf(error) };
    }
    this.pendingCreate = null;
    if (mine !== this.flight) return { ok: false, link: false, message: "superseded" };
    this.adopt(created.session_id, "create", {
      title: typeof params.title === "string" ? params.title : null,
      messages: [],
    });
    const mode = toolProgressMode(this.deps.env);
    if (typeof mode === "object" && mode !== null) {
      this.deps.system(
        `tool progress mode ${mode.invalid} is not one of off, new, all, verbose; the configured mode stays`,
      );
    } else if (mode !== null) {
      try {
        await this.deps.port.call("config.set", {
          key: "verbose",
          value: mode,
          session_id: created.session_id,
        });
      } catch (error) {
        this.deps.system(`tool progress mode ${mode} not applied: ${messageOf(error)}`);
      }
    }
    return { ok: true, sessionId: created.session_id };
  }

  /** One create, repeated once with the same key when the answer did not come in time. */
  private async createOnce(params: object): Promise<SessionCreateResult> {
    try {
      return (await this.deps.port.call("session.create", params)) as SessionCreateResult;
    } catch (error) {
      if (!(error instanceof RpcTimeout)) throw error;
      return (await this.deps.port.call("session.create", params)) as SessionCreateResult;
    }
  }

  /** Make `id` the open session: transcript, focus, title, active-session file. */
  private adopt(
    id: string,
    reason: OpenReason,
    loaded: { title: string | null; messages: readonly HistoryMessage[] },
  ): void {
    this.deps.resetTurn();
    const transcript = this.transcript.getState();
    transcript.clear();
    for (const row of historyRows(loaded.messages)) transcript.append(row);
    const info = this.deps.infoFor?.(id) ?? null;
    focusSession(id);
    const infoTitle = typeof info?.title === "string" ? info.title : null;
    this.ui.getState().patch({
      info,
      title: loaded.title ?? infoTitle,
      busy: info?.running === true,
      compacting: false,
      usage: null,
      statusText: STATUS_READY,
    });
    const model = typeof info?.model === "string" ? info.model : null;
    this.live.delete(id);
    this.live.set(id, { id, title: loaded.title ?? infoTitle, model });
    this.writeActiveSession(id);
    for (const line of this.carried.splice(0)) this.deps.system(line);
    this.deps.onOpened?.(id, reason);
  }

  /** A line to show once the next session opens (opening clears the transcript). */
  carry(line: string): void {
    this.carried.push(line);
  }

  /** Keep the live row's title and model in step with `session.info`. */
  noteInfo(id: string, info: Record<string, unknown>): void {
    const row = this.live.get(id);
    if (row === undefined) return;
    if (typeof info.title === "string") row.title = info.title;
    if (typeof info.model === "string") row.model = info.model;
  }

  private writeActiveSession(id: string): void {
    const file = this.deps.env[ENV.activeSessionFile]?.trim();
    if (!file) return;
    const write =
      this.deps.writeFile ??
      ((path: string, text: string) => writeFileSync(path, text, { mode: 0o600 }));
    try {
      write(file, JSON.stringify({ session_id: id }));
    } catch {
      // only the launcher's exit hint reads it; never break a session change over it
    }
  }

  /** The launcher's one-shot prompt, sent literally once a session is open. */
  private async sendStartupPrompt(): Promise<void> {
    if (this.startupDone) return;
    const prompt: StartupPrompt | null = startupPrompt(this.deps.env);
    const sid = this.ui.getState().sid;
    if (prompt === null || sid === null) return;
    this.startupDone = true;
    if (prompt.image !== null) {
      try {
        await this.deps.port.call("image.attach", { session_id: sid, path: prompt.image });
      } catch (error) {
        this.deps.system(`startup prompt not sent: the image did not attach (${messageOf(error)})`);
        return;
      }
    }
    if (this.ui.getState().sid !== sid) {
      this.deps.system("startup prompt not sent: the session changed first");
      return;
    }
    this.deps.userLine(prompt.text);
    this.ui.getState().patch({ busy: true, statusText: "sending…" });
    try {
      await this.deps.port.call("prompt.submit", { session_id: sid, text: prompt.text });
    } catch (error) {
      if (this.ui.getState().sid === sid) {
        this.deps.system(`error: ${messageOf(error)}`);
        this.ui.getState().patch({ busy: false, statusText: STATUS_READY });
      }
    }
  }

  /** True (and a line said) when a turn runs, so switching must wait. */
  refuseWhileBusy(): boolean {
    if (!this.ui.getState().busy) return false;
    this.deps.system(BUSY_SWITCH_LINE);
    return true;
  }

  /** `/new`, `/clear`, the switcher's new row: a fresh session; `keepCurrent` leaves it live. */
  async newSession(title?: string, keepCurrent = false): Promise<OpenResult | null> {
    if (this.refuseWhileBusy()) return null;
    const previous = this.ui.getState().sid;
    this.status(STATUS_STARTING_SESSION);
    const result = await this.create(title);
    if (result.ok && previous !== null && previous !== result.sessionId && !keepCurrent) {
      void this.close(previous);
    }
    return result;
  }

  /** Open another session (live or stored); refused while a turn runs. */
  async switchTo(id: string): Promise<OpenResult | null> {
    if (this.refuseWhileBusy()) return null;
    if (this.ui.getState().sid === id) return { ok: true, sessionId: id };
    this.status(STATUS_SWITCHING);
    const result = await this.open(id, "switch");
    if (!result.ok && result.message !== "superseded") {
      this.deps.system(`error: could not open session ${id}: ${result.message}`);
    }
    return result;
  }

  /** Detach a live session (the stored row stays). */
  async close(id: string): Promise<boolean> {
    this.live.delete(id);
    try {
      const reply = (await this.deps.port.call("session.close", { session_id: id })) as {
        closed?: boolean;
      } | null;
      return reply?.closed === true;
    } catch {
      return false;
    }
  }

  /** The switcher's Ctrl+D: close a live row; closing the open one moves to another or a new one. */
  async closeLive(id: string): Promise<void> {
    const current = this.ui.getState().sid === id;
    if (current && this.refuseWhileBusy()) return;
    await this.close(id);
    if (!current) return;
    const next = this.liveSessions()[0];
    if (next !== undefined) await this.switchTo(next.id);
    else await this.newSession(undefined, true);
  }

  /** Stored sessions for the switcher, newest first, delegated runs hidden. */
  async listStored(): Promise<SessionRow[]> {
    const result = (await this.deps.port.call("session.list", {
      limit: STORED_LIMIT,
    })) as SessionListResult | null;
    return visibleStored(result?.sessions ?? []);
  }

  /** The switcher's `d d`. Resolves to the line to show. */
  async deleteStored(id: string): Promise<string> {
    if (this.live.has(id)) return "Close the live session first (Ctrl+D), then delete it.";
    try {
      await this.deps.port.call("session.delete", { session_id: id });
      return `Deleted ${id}.`;
    } catch (error) {
      if (error instanceof GatewayRpcError && error.errorName === "METHOD_NOT_FOUND") {
        return "Deleting stored sessions is not available in this agent yet.";
      }
      return `Could not delete ${id}: ${messageOf(error)}`;
    }
  }
}
