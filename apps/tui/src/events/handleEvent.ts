// One switch from client events to store updates. Two filters run first: events of another
// session than the focused one, and events whose `seq` was already applied (a replay). Reply
// text is always appended raw; a rendered fragment never replaces it (R05.6).

import type { ClientEvent } from "@indus/client";
import { overlayStore } from "../state/overlayStore.ts";
import { transcriptStore } from "../state/transcriptStore.ts";
import { turnStore } from "../state/turnStore.ts";
import { STATUS_READY, type UiState, uiStore } from "../state/uiStore.ts";
import { applyThemePayload } from "../theme/theme.ts";
import type { TurnController } from "../turn/controller.ts";
import {
  GOAL_STATUS_RESTORE_MS,
  NOISE_PREVIEW_CHARS,
  PROTOCOL_WARNING_MS,
  STATUS_RESTORE_MS,
} from "./timing.ts";

export interface EventHandlerOptions {
  controller: TurnController;
  /** Session bootstrap after the bridge is ready (P12-27 owns it). */
  onReady?: (event: Extract<ClientEvent, { type: "gateway.ready" }>) => void;
  /** Where system lines go (default: the transcript store). */
  sys?: (text: string) => void;
  /** The user pinned light/dark in the environment: a server theme name does not override it. */
  themePinned?: boolean;
}

export type EventHandler = (event: ClientEvent) => void;

/** Status kinds that also leave a line in the activity lane. */
const NOISY_STATUS: ReadonlySet<string> = new Set(["waiting", "retrying", "compressing"]);

/** stderr lines worth a warning: everything else stays in the log ring only. */
const FAILURE_LINE = /(error|exception)\b|\b(traceback|fatal|failed|failure|panic|critical)\b/i;

const STATUS_LABELS: Readonly<Record<string, string>> = {
  working: "working…",
  waiting: "waiting…",
  retrying: "retrying…",
  compressing: "compacting context…",
  ready: STATUS_READY,
};

const GOAL_LABELS: Readonly<Record<string, string>> = {
  active: "goal in progress",
  paused: "goal paused",
  done: "goal reached",
  cleared: "goal cleared",
};

function rec(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null ? (value as Record<string, unknown>) : {};
}

function str(value: unknown): string {
  return typeof value === "string" ? value : "";
}

/** Shallow equality on the counters, so an unchanged usage tick does not re-render. */
function sameUsage(a: Record<string, unknown> | null, b: Record<string, unknown>): boolean {
  if (a === null) return false;
  const keys = new Set([...Object.keys(a), ...Object.keys(b)]);
  for (const key of keys) if (a[key] !== b[key]) return false;
  return true;
}

export function createEventHandler(options: EventHandlerOptions): EventHandler {
  const { controller } = options;
  const sys =
    options.sys ?? ((text: string) => transcriptStore.getState().append({ role: "system", text }));
  const lastSeq = new Map<string, number>();
  let protocolWarned = false;

  const ui = () => uiStore.getState();

  /** Drop events of another session and repeats of an already applied seq. */
  const admit = (event: ClientEvent): boolean => {
    const body = event as { session_id?: unknown; seq?: unknown };
    if (typeof body.session_id !== "string") return true;
    const sid = ui().sid;
    if (sid !== null && body.session_id !== sid) return false;
    if (typeof body.seq === "number") {
      const seen = lastSeq.get(body.session_id);
      if (seen !== undefined && body.seq <= seen) return false;
      lastSeq.set(body.session_id, body.seq);
    }
    return true;
  };

  return (event) => {
    if (!admit(event)) return;
    switch (event.type) {
      case "gateway.ready": {
        lastSeq.clear();
        protocolWarned = false;
        const theme = str(rec(event).theme) || "auto";
        uiStore.getState().patch({ connected: true, themeName: theme });
        applyThemePayload(options.themePinned ? "auto" : theme);
        options.onReady?.(event);
        return;
      }
      case "session.info": {
        const { type: _type, seq: _seq, ...info } = event;
        const patch: Partial<UiState> = { info };
        if (info.title !== undefined) patch.title = info.title ?? null;
        if (info.running === true && !ui().busy) patch.busy = true;
        uiStore.getState().patch(patch);
        return;
      }
      case "session.title":
        uiStore.getState().patch({ title: event.title ?? null });
        return;
      case "session.usage": {
        const usage = rec(event.usage);
        if (!sameUsage(ui().usage, usage)) uiStore.getState().patch({ usage });
        return;
      }
      case "turn.started":
        controller.start(event.turn_id, event.generation);
        return;
      case "message.delta":
        // Only `text` is read: a `rendered` field, if a server ever sends one, is ignored (R05.6).
        controller.delta(event.turn_id, event.text);
        return;
      case "reasoning.delta":
        controller.reasoning(event.turn_id, event.text);
        return;
      case "tool.started":
        controller.toolStart(event.turn_id, event.call_id, event.name, event.arguments);
        return;
      case "tool.progress":
        controller.toolProgress(event.turn_id, event.call_id, event.message);
        return;
      case "tool.completed":
        controller.toolComplete(event.turn_id, {
          callId: event.call_id,
          name: event.name,
          ok: event.ok,
          durationMs: event.duration_ms,
          result: event.result,
        });
        return;
      case "todo.updated":
        controller.todos(event.todos);
        return;
      case "status": {
        const label = event.text?.trim() || STATUS_LABELS[event.kind] || event.kind;
        if (event.kind === "compressing") uiStore.getState().patch({ compacting: true });
        if (event.kind === "ready") {
          uiStore.getState().patch({ compacting: false });
          controller.setStatus(ui().busy ? (STATUS_LABELS.working ?? label) : STATUS_READY);
          return;
        }
        if (event.kind === "working") {
          controller.setStatus(label);
          return;
        }
        controller.setStatus(label, STATUS_RESTORE_MS);
        if (NOISY_STATUS.has(event.kind)) {
          controller.pushActivity(label, event.kind === "retrying" ? "warn" : "info", "status");
        }
        return;
      }
      case "notice":
        controller.showNotice({ key: event.key ?? null, level: event.level, text: event.text });
        return;
      case "server_request.cancelled": {
        const open = [overlayStore.getState().card, ...overlayStore.getState().waiting].find(
          (card) => card?.requestId === event.request_id,
        );
        overlayStore.getState().closeByRequest(event.request_id);
        if (event.reason === "timed_out" && (open?.kind === "sudo" || open?.kind === "secret")) {
          sys(
            open.kind === "sudo"
              ? "The password prompt timed out and the command did not run. Ask again to get a new prompt."
              : "The secret prompt timed out and nothing was saved. Ask again to get a new prompt.",
          );
        }
        return;
      }
      case "turn.done":
        controller.done(event.turn_id, event.reason, event.failure);
        return;
      case "message.complete":
        controller.complete(event.turn_id, event.text, event.reason);
        return;
      case "goal.update": {
        const brief = GOAL_LABELS[event.status] ?? "goal updated";
        sys(
          event.reason
            ? `Goal ${event.status}: ${event.goal} (${event.reason})`
            : `Goal ${event.status}: ${event.goal}`,
        );
        controller.setStatus(brief, GOAL_STATUS_RESTORE_MS);
        return;
      }
      case "loop.update":
        controller.pushActivity(
          `loop ${event.status}: ${event.prompt} (${event.ticks} run${event.ticks === 1 ? "" : "s"})`,
          event.status === "stopped" ? "warn" : "info",
          `loop-${event.loop_id}`,
        );
        return;
      case "heartbeat.fired":
        sys(`Heartbeat woke the session: ${event.prompt}`);
        return;
      case "learning.review.done":
        for (const line of event.summary_lines ?? []) sys(line);
        return;
      case "transport.stderr":
        if (FAILURE_LINE.test(event.line)) {
          controller.pushActivity(`bridge: ${event.line.trim().slice(0, 160)}`, "warn", "stderr");
        }
        return;
      case "transport.protocol_error":
        controller.setStatus("protocol warning", PROTOCOL_WARNING_MS);
        if (!protocolWarned) {
          protocolWarned = true;
          controller.pushActivity(
            "The bridge printed something that is not protocol; it was ignored.",
          );
        }
        if (event.preview) {
          controller.pushActivity(
            `noise: ${event.preview.slice(0, NOISE_PREVIEW_CHARS)}`,
            "info",
            "noise",
          );
        }
        return;
      case "transport.start_timeout": {
        controller.setStatus("bridge did not start");
        const tail = event.stderr_tail.slice(-5).join("\n");
        sys(`The bridge (${event.python}) did not get ready in time.${tail ? `\n${tail}` : ""}`);
        return;
      }
      case "transport.exit":
        lastSeq.clear();
        controller.reset();
        uiStore.getState().patch({ connected: false });
        controller.setStatus("bridge stopped");
        return;
      case "transport.reconnecting":
        controller.setStatus(`reconnecting (attempt ${event.attempt})…`);
        return;
      default:
        // Events this renderer does not show yet are ignored on purpose.
        return;
    }
  };
}

/** A rejected call is never a turn end: one system line, busy untouched. */
export function reportRpcFailure(what: string, error: unknown, sys?: (text: string) => void): void {
  const message = error instanceof Error ? error.message : String(error);
  (sys ?? ((text: string) => transcriptStore.getState().append({ role: "system", text })))(
    `${what} failed: ${message}`,
  );
}

/** A compact view of every store, for golden snapshots. */
export function storeSnapshot(): Record<string, unknown> {
  const u = uiStore.getState();
  const t = turnStore.getState();
  const o = overlayStore.getState();
  return {
    ui: {
      busy: u.busy,
      statusText: u.statusText,
      title: u.title,
      usage: u.usage,
      notice:
        u.notice === null
          ? null
          : { level: u.notice.level, text: u.notice.text, key: u.notice.key },
      connected: u.connected,
    },
    turn: {
      streamBuffer: t.streamBuffer,
      segments: t.segments.length,
      activeTools: t.activeTools.length,
      todos: t.todos,
      activity: t.activity.map((a) => `${a.level}: ${a.text}`),
      reasoning: t.reasoning.length,
      outcome: t.outcome,
    },
    overlay: { card: o.card?.kind ?? null, waiting: o.waiting.length },
    transcript: transcriptStore.getState().messages.map((m) => ({
      role: m.role,
      kind: m.kind ?? "text",
      text: m.text.length > 200 ? `${m.text.slice(0, 120)}…(${m.text.length} chars)` : m.text,
      tools: (m.tools ?? []).map((tool) => `${tool.name}:${tool.status}`),
      ...(m.failure ? { failure: m.failure } : {}),
    })),
  };
}
