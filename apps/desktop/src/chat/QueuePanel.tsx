// The queue section of the composer's status stack (P20-21): the daemon's queued inputs, the
// inputs this window holds while the queue is paused, a lost turn's single Discard, and journaled
// inputs whose last send had no definite answer.

import { useSyncExternalStore } from "react";
import { useT } from "../i18n/index.tsx";
import type { BusySnapshot, ChatActions } from "./busyActions.ts";

export function useBusySnapshot(actions: ChatActions): BusySnapshot {
  return useSyncExternalStore(actions.subscribe, actions.snapshot, actions.snapshot);
}

const ROW = "flex items-center gap-2 py-1 text-sm";
const BUTTON =
  "rounded px-2 py-0.5 text-xs border border-line hover:bg-raised focus-visible:outline-2 focus-visible:outline-focus";

export function QueuePanel({ actions }: { actions: ChatActions }) {
  const t = useT();
  const state = useBusySnapshot(actions);
  const waiting = state.queue.length + state.held.length;
  if (waiting === 0 && state.lost === null && state.uncertain.length === 0) return null;

  const header =
    state.lost !== null
      ? t("queue.blocked", { count: waiting })
      : state.paused
        ? t("queue.paused", { count: waiting })
        : t("queue.waiting", { count: waiting });

  return (
    <section aria-label={t("queue.label")} className="px-3 py-2" data-testid="queue-panel">
      <div className="flex items-center gap-2 text-xs text-muted">
        <span>{header}</span>
        {state.paused && state.lost === null ? (
          <button type="button" className={BUTTON} onClick={() => void actions.resume()}>
            {t("queue.resume")}
          </button>
        ) : null}
      </div>

      {state.lost !== null ? (
        <div className={ROW} role="alert">
          <span className="flex-1">{t("queue.lostTurn")}</span>
          <button type="button" className={BUTTON} onClick={() => void actions.discard()}>
            {t("queue.discard")}
          </button>
        </div>
      ) : null}

      <ol className="list-none p-0 m-0">
        {state.queue.map((entry) => (
          <li key={entry.admissionId} className={ROW} data-state="queued">
            <span className="flex-1 truncate">{entry.text ?? t("queue.otherSurface")}</span>
            {entry.mine ? (
              <button
                type="button"
                className={BUTTON}
                aria-label={t("queue.removeItem")}
                onClick={() => void actions.cancelQueued(entry.admissionId)}
              >
                {t("queue.remove")}
              </button>
            ) : null}
          </li>
        ))}
        {state.held.map((item) => (
          <li key={item.requestId} className={ROW} data-state={item.failed ? "failed" : "held"}>
            <span className="flex-1 truncate">{item.text}</span>
            {item.failed ? <span className="text-danger text-xs">{t("queue.gaveUp")}</span> : null}
            <button
              type="button"
              className={BUTTON}
              aria-label={t("queue.removeItem")}
              onClick={() => actions.removeHeld(item.requestId)}
            >
              {t("queue.remove")}
            </button>
          </li>
        ))}
      </ol>

      {state.uncertain.map((item) => (
        <div key={item.requestId} className={ROW} data-state="uncertain">
          <span className="flex-1 truncate">{t("queue.uncertain", { text: item.text })}</span>
          <button
            type="button"
            className={BUTTON}
            onClick={() => void actions.retryUncertain(item.requestId)}
          >
            {t("queue.retry")}
          </button>
          <button
            type="button"
            className={BUTTON}
            onClick={() => void actions.dismissUncertain(item.requestId)}
          >
            {t("queue.dismiss")}
          </button>
        </div>
      ))}
    </section>
  );
}
