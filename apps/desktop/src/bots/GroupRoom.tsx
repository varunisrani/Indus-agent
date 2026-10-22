// A group room, text only (P24-31 over P24-05/06's `groups.*` methods). The log is read once with
// `groups.log`; after that the room follows `groups.watch` (wave 48, RS3): the daemon pushes every
// new event as a `groups.event` notification, a reconnect (`gateway.ready`) watches again from the
// last sequence number seen, and closing the room sends `groups.unwatch`. Only when the watch is
// refused (an older daemon, or a connection without notifications) does it poll `groups.log`.
// The person's messages go out with `groups.send` under a client event id that a retry reuses (a
// lost answer never posts twice); Stop sends `groups.stop` with a fresh cancel id. What the person
// may do next comes only from the driver's `groups.status` (`pending_actions`: Retry of a deferred
// or indeterminate turn, sent with exactly that task id). Disband needs an explicit second
// confirmation. An approval a member's turn asked (`approval.requested`) is a card offering exactly
// the request's choices, answered with `groups.approve`; it closes once the room records an answer
// or the request settled. The member's command and description are its data (SEC-15): shown as
// quoted text under the member's name, never as the room's or the person's own words.

import { useCallback, useEffect, useRef, useState } from "react";
import { useT } from "../i18n/index.tsx";
import type { RpcLike } from "./canonicalChat.ts";
import { allowedRoomChoices, RoomApprovalCard } from "./RoomApprovalCard.tsx";

export const ROOM_POLL_MS = 2_000;
const GROUPS_EVENT = "groups.event";
const READY = "gateway.ready";

export interface RoomEvent {
  seq: number;
  event_id: string;
  kind: string;
  actor: { kind?: string; id?: string };
  payload: Record<string, unknown>;
  created_at?: number;
}

interface LogPage {
  events: RoomEvent[];
  latest_seq: number;
  has_more: boolean;
}

interface RoomStatus {
  working: boolean;
  blocked: boolean;
  pending_actions: Array<{ kind: string; task_id: string }>;
}

/** An approval a member's turn put to the room and nobody has answered or settled yet. */
export interface RoomApproval {
  requestId: string;
  member: string;
  roomName: string;
  command: string;
  description: string;
  choices: string[];
}

function newClientId(): string {
  return `c-${crypto.randomUUID()}`;
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function isRoomEvent(value: unknown): value is RoomEvent {
  if (typeof value !== "object" || value === null) return false;
  const event = value as Partial<RoomEvent>;
  return (
    typeof event.seq === "number" &&
    typeof event.kind === "string" &&
    typeof event.payload === "object" &&
    event.payload !== null
  );
}

/** How one room event reads as a line of text. */
export function lineOf(event: RoomEvent): { who: string; text: string; system: boolean } {
  const said = text(event.payload.text);
  if (event.kind === "message.user")
    return { who: event.actor.id ?? "you", text: said, system: false };
  if (event.kind === "message.member")
    return { who: event.actor.id ?? "bot", text: said, system: false };
  const member = typeof event.payload.member === "string" ? ` ${event.payload.member}` : "";
  return { who: "", text: `${event.kind.replace(/[._]/g, " ")}${member}`, system: true };
}

/** The approvals still open in a room's events: asked, with no answer and not settled. */
export function openApprovals(events: readonly RoomEvent[]): RoomApproval[] {
  const closed = new Set<string>();
  for (const event of events) {
    if (event.kind === "approval.answered" || event.kind === "approval.settled") {
      closed.add(text(event.payload.request_id));
    }
  }
  const open: RoomApproval[] = [];
  for (const event of events) {
    if (event.kind !== "approval.requested") continue;
    const requestId = text(event.payload.request_id);
    if (requestId === "" || closed.has(requestId)) continue;
    const offered = Array.isArray(event.payload.choices) ? event.payload.choices : [];
    open.push({
      requestId,
      member: text(event.payload.member) || "member",
      roomName: text(event.payload.room_name),
      command: text(event.payload.command),
      description: text(event.payload.description),
      choices: allowedRoomChoices(offered.map(String)),
    });
  }
  return open;
}

export function GroupRoom(props: {
  rpc: RpcLike;
  roomId: string;
  pollMs?: number;
  newId?: () => string;
  onDisbanded?: () => void;
  /** The approval card's arming delay (tests pass 0). */
  armMs?: number;
}) {
  const t = useT();
  const { rpc, roomId } = props;
  const pollMs = props.pollMs ?? ROOM_POLL_MS;
  const makeId = props.newId ?? newClientId;
  const [events, setEvents] = useState<RoomEvent[]>([]);
  const [status, setStatus] = useState<RoomStatus | null>(null);
  const [draft, setDraft] = useState("");
  const [pendingSend, setPendingSend] = useState<{ id: string; text: string } | null>(null);
  const [error, setError] = useState("");
  const [confirmDisband, setConfirmDisband] = useState(false);
  const since = useRef(0);
  const shownRoom = useRef(roomId);
  const statusAsked = useRef<{ running: boolean; again: boolean }>({
    running: false,
    again: false,
  });

  const merge = useCallback((incoming: readonly RoomEvent[]) => {
    if (incoming.length === 0) return;
    since.current = Math.max(since.current, ...incoming.map((e) => e.seq));
    setEvents((current) => {
      const known = new Set(current.map((e) => e.seq));
      const fresh = incoming.filter((e) => !known.has(e.seq));
      if (fresh.length === 0) return current;
      return [...current, ...fresh].sort((x, y) => x.seq - y.seq);
    });
  }, []);

  const failed = useCallback(
    (failure: unknown) => setError(failure instanceof Error ? failure.message : t("manage.failed")),
    [t],
  );

  /** `groups.status` again; calls made while one is in flight collapse into one more. */
  const refreshStatus = useCallback(async () => {
    const asked = statusAsked.current;
    if (asked.running) {
      asked.again = true;
      return;
    }
    asked.running = true;
    try {
      do {
        asked.again = false;
        setStatus((await rpc.request("groups.status", { room_id: roomId })) as RoomStatus);
      } while (asked.again);
    } catch (failure) {
      failed(failure);
    } finally {
      asked.running = false;
    }
  }, [rpc, roomId, failed]);

  const refresh = useCallback(async () => {
    try {
      for (let pages = 0; pages < 20; pages += 1) {
        const page = (await rpc.request("groups.log", {
          room_id: roomId,
          since_seq: since.current,
        })) as LogPage;
        merge(page.events);
        if (!page.has_more) break;
      }
    } catch (failure) {
      failed(failure);
      return;
    }
    await refreshStatus();
  }, [rpc, roomId, merge, failed, refreshStatus]);

  useEffect(() => {
    if (shownRoom.current !== roomId) {
      // Another room in the same view: nothing of the last one carries over.
      shownRoom.current = roomId;
      since.current = 0;
      setEvents([]);
      setStatus(null);
    }
    let closed = false;
    let watching = false;
    let timer: ReturnType<typeof setInterval> | undefined;
    const poll = () => {
      if (closed || timer !== undefined) return;
      timer = setInterval(() => void refresh(), pollMs);
    };
    const watch = async () => {
      try {
        await rpc.request("groups.watch", { room_id: roomId, since_seq: since.current });
      } catch {
        watching = false;
        poll(); // refused (an older daemon): the log is polled instead
        return;
      }
      watching = true;
      if (closed) void rpc.request("groups.unwatch", { room_id: roomId }).catch(() => undefined);
    };
    const off = rpc.onEvent?.((event) => {
      if (event.type === GROUPS_EVENT) {
        if (event.room_id !== roomId || !isRoomEvent(event.event)) return;
        merge([event.event]);
        void refreshStatus();
      } else if (event.type === READY && watching && !closed) {
        void watch(); // a new connection: the daemon's watch ended with the old one
      }
    });
    void (async () => {
      await refresh();
      if (closed) return;
      if (off === undefined) poll();
      else await watch();
    })();
    return () => {
      closed = true;
      off?.();
      if (timer !== undefined) clearInterval(timer);
      if (watching) void rpc.request("groups.unwatch", { room_id: roomId }).catch(() => undefined);
    };
  }, [rpc, roomId, pollMs, refresh, refreshStatus, merge]);

  const approve = async (requestId: string, choice: string, reason: string) => {
    const params: Record<string, unknown> = { room_id: roomId, request_id: requestId, choice };
    if (choice === "deny" && reason !== "") params.reason = reason;
    const answer = (await rpc.request("groups.approve", params)) as { event?: unknown };
    if (isRoomEvent(answer?.event)) merge([answer.event]);
  };

  const send = async (attempt: { id: string; text: string }) => {
    setPendingSend(attempt);
    try {
      await rpc.request("groups.send", {
        room_id: roomId,
        client_event_id: attempt.id,
        text: attempt.text,
      });
      setPendingSend(null);
      setDraft("");
      setError("");
      await refresh();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : t("manage.failed"));
    }
  };

  const stop = async () => {
    try {
      await rpc.request("groups.stop", { room_id: roomId, cancel_id: makeId() });
      await refresh();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : t("manage.failed"));
    }
  };

  const retry = async (taskId: string) => {
    try {
      await rpc.request("groups.retry", { room_id: roomId, task_id: taskId });
      await refresh();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : t("manage.failed"));
    }
  };

  const disband = async () => {
    setConfirmDisband(false);
    try {
      await rpc.request("groups.disband", { room_id: roomId });
      props.onDisbanded?.();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : t("manage.failed"));
    }
  };

  return (
    <div className="manage-card" data-room={roomId}>
      <div className="room-log" aria-live="polite">
        {events.map((event) => {
          const line = lineOf(event);
          return (
            <p key={event.seq} className="room-line" data-kind={line.system ? "system" : "message"}>
              {line.who !== "" && <strong>{line.who}: </strong>}
              {line.text}
            </p>
          );
        })}
        {events.length === 0 && <p className="manage-muted">{t("manage.room.empty")}</p>}
      </div>
      {openApprovals(events).map((ask) => (
        <RoomApprovalCard
          key={ask.requestId}
          ask={ask}
          armMs={props.armMs}
          onAnswer={(choice, reason) => approve(ask.requestId, choice, reason)}
        />
      ))}
      {status !== null && (
        <div className="manage-inline" role="status">
          {status.working && (
            <span className="manage-badge" data-tone="ok">
              {t("manage.room.working")}
            </span>
          )}
          {status.blocked && (
            <span className="manage-badge" data-tone="warn">
              {t("manage.room.blocked")}
            </span>
          )}
          {status.pending_actions
            .filter((action) => action.kind === "retry")
            .map((action) => (
              <button
                key={action.task_id}
                type="button"
                className="button button-secondary"
                onClick={() => void retry(action.task_id)}
              >
                {t("manage.room.retry", { task: action.task_id.slice(0, 8) })}
              </button>
            ))}
        </div>
      )}
      {error !== "" && (
        <p className="manage-error" role="alert">
          {error}
        </p>
      )}
      <form
        className="manage-inline"
        onSubmit={(e) => {
          e.preventDefault();
          const text = draft.trim();
          if (text === "") return;
          void send(
            pendingSend !== null && pendingSend.text === text
              ? pendingSend
              : { id: makeId(), text },
          );
        }}
      >
        <input
          aria-label={t("manage.room.message")}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
        />
        <button type="submit" className="button button-primary">
          {pendingSend !== null && pendingSend.text === draft.trim()
            ? t("manage.room.resend")
            : t("manage.room.send")}
        </button>
        <button type="button" className="button button-secondary" onClick={() => void stop()}>
          {t("manage.room.stop")}
        </button>
        <button
          type="button"
          className="button button-secondary"
          onClick={() => setConfirmDisband(true)}
        >
          {t("manage.room.disband")}
        </button>
      </form>
      {confirmDisband && (
        <div
          className="manage-banner"
          role="alertdialog"
          aria-label={t("manage.room.disbandConfirm")}
        >
          <span>{t("manage.room.disbandConfirm")}</span>
          <button type="button" className="button button-primary" onClick={() => void disband()}>
            {t("manage.room.disbandYes")}
          </button>
          <button
            type="button"
            className="button button-secondary"
            onClick={() => setConfirmDisband(false)}
          >
            {t("manage.cancel")}
          </button>
        </div>
      )}
    </div>
  );
}
