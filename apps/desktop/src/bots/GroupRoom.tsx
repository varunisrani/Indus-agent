// A group room, text only (P24-31 over P24-05/06's `groups.*` methods). The log is read with
// `groups.log` from the last seen sequence number; the person's messages go out with
// `groups.send` under a client event id that a retry reuses (a lost answer never posts twice);
// Stop sends `groups.stop` with a fresh cancel id. What the person may do next comes only from the
// driver's `groups.status` (`pending_actions`: Retry of a deferred or indeterminate turn, sent with
// exactly that task id). Disband needs an explicit second confirmation.

import { useCallback, useEffect, useRef, useState } from "react";
import { useT } from "../i18n/index.tsx";
import type { RpcLike } from "./canonicalChat.ts";

export const ROOM_POLL_MS = 2_000;

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

function newClientId(): string {
  return `c-${crypto.randomUUID()}`;
}

/** How one room event reads as a line of text. */
export function lineOf(event: RoomEvent): { who: string; text: string; system: boolean } {
  const text = typeof event.payload.text === "string" ? event.payload.text : "";
  if (event.kind === "message.user") return { who: event.actor.id ?? "you", text, system: false };
  if (event.kind === "message.member") return { who: event.actor.id ?? "bot", text, system: false };
  const member = typeof event.payload.member === "string" ? ` ${event.payload.member}` : "";
  return { who: "", text: `${event.kind.replace(/[._]/g, " ")}${member}`, system: true };
}

export function GroupRoom(props: {
  rpc: RpcLike;
  roomId: string;
  pollMs?: number;
  newId?: () => string;
  onDisbanded?: () => void;
}) {
  const t = useT();
  const { rpc, roomId } = props;
  const makeId = props.newId ?? newClientId;
  const [events, setEvents] = useState<RoomEvent[]>([]);
  const [status, setStatus] = useState<RoomStatus | null>(null);
  const [draft, setDraft] = useState("");
  const [pendingSend, setPendingSend] = useState<{ id: string; text: string } | null>(null);
  const [error, setError] = useState("");
  const [confirmDisband, setConfirmDisband] = useState(false);
  const since = useRef(0);

  const refresh = useCallback(async () => {
    try {
      for (let pages = 0; pages < 20; pages += 1) {
        const page = (await rpc.request("groups.log", {
          room_id: roomId,
          since_seq: since.current,
        })) as LogPage;
        if (page.events.length > 0) {
          since.current = Math.max(since.current, ...page.events.map((e) => e.seq));
          setEvents((current) => {
            const known = new Set(current.map((e) => e.seq));
            return [...current, ...page.events.filter((e) => !known.has(e.seq))].sort(
              (a, b) => a.seq - b.seq,
            );
          });
        }
        if (!page.has_more) break;
      }
      setStatus((await rpc.request("groups.status", { room_id: roomId })) as RoomStatus);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : t("manage.failed"));
    }
  }, [rpc, roomId, t]);

  useEffect(() => {
    void refresh();
    const timer = setInterval(() => void refresh(), props.pollMs ?? ROOM_POLL_MS);
    return () => clearInterval(timer);
  }, [refresh, props.pollMs]);

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
