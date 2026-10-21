// The Agents overlay (P24-28): the live delegation of the conversation it was opened over —
// each `delegate_task` batch, its workers (goal, status, elapsed time, tools, files) and the
// active counts. Fed by the session's events through the window's connection (see
// `delegation.ts` for how tool events become workers).

import { useEffect, useReducer } from "react";
import { useT } from "../../i18n/index.tsx";
import { ManageOverlay } from "../manage/common.tsx";
import {
  type DelegationEvent,
  type DelegationState,
  EMPTY_DELEGATION,
  reduceDelegation,
  totals,
} from "./delegation.ts";

export interface SessionEventSource {
  onSessionEvent(sessionId: string, listener: (event: DelegationEvent) => void): () => void;
}

function elapsed(from: number, to: number | null, now: number): string {
  const seconds = Math.max(0, Math.round(((to ?? now) - from) / 1000));
  return seconds < 60 ? `${seconds} s` : `${Math.floor(seconds / 60)} min ${seconds % 60} s`;
}

export function DelegationView(props: { state: DelegationState; now: number }) {
  const t = useT();
  const sum = totals(props.state);
  if (props.state.batches.length === 0) {
    return <p className="manage-muted">{t("manage.agents.none")}</p>;
  }
  return (
    <>
      <p role="status" data-testid="agents-totals">
        {t("manage.agents.totals", {
          workers: sum.workers,
          active: sum.active,
          tools: sum.tools,
          files: sum.files,
        })}
      </p>
      {props.state.batches.map((batch) => (
        <div className="manage-card" key={batch.callId}>
          <h3>{t("manage.agents.batch", { count: batch.workers.length })}</h3>
          <table className="manage-table">
            <tbody>
              {batch.workers.map((worker) => (
                <tr key={worker.index} data-status={worker.status}>
                  <td>{worker.index}</td>
                  <td>
                    <strong>{worker.goal}</strong>
                    {worker.lastNote !== "" && (
                      <div className="manage-muted">{worker.lastNote}</div>
                    )}
                    {worker.files.length > 0 && (
                      <div className="manage-muted">{worker.files.slice(0, 8).join(", ")}</div>
                    )}
                  </td>
                  <td>{t(`manage.agents.status.${worker.status}`)}</td>
                  <td>{elapsed(worker.startedAt, worker.endedAt, props.now)}</td>
                  <td>{t("manage.agents.tools", { count: worker.tools })}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
    </>
  );
}

export function AgentsOverlay(props: {
  connection: SessionEventSource | null;
  sessionId: string | null;
  onClose: () => void;
}) {
  const t = useT();
  const [state, dispatch] = useReducer(
    (current: DelegationState, event: DelegationEvent) =>
      reduceDelegation(current, event, Date.now()),
    EMPTY_DELEGATION,
  );
  const [now, tick] = useReducer(() => Date.now(), Date.now());
  const { connection, sessionId } = props;
  useEffect(() => {
    if (connection === null || sessionId === null) return;
    return connection.onSessionEvent(sessionId, dispatch);
  }, [connection, sessionId]);
  useEffect(() => {
    const timer = setInterval(tick, 1000);
    return () => clearInterval(timer);
  }, []);
  return (
    <ManageOverlay title={t("nav.agents")} onClose={props.onClose}>
      {sessionId === null ? (
        <p className="manage-muted">{t("manage.agents.noSession")}</p>
      ) : (
        <DelegationView state={state} now={now} />
      )}
    </ManageOverlay>
  );
}
