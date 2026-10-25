// The Agents overlay (P24-28): the live delegation of the conversation it was opened over —
// each `delegate_task` batch, its workers (goal, status, elapsed time, tools, files) and the
// active counts. Fed by the session's events through the window's connection (see
// `delegation.ts` for how tool events become workers).

import { AlertCircle, Bot, CheckCircle2, CircleDashed, Loader2 } from "lucide-react";
import { useEffect, useReducer, useState } from "react";
import { useT } from "../../i18n/index.tsx";
import { EmptyState } from "../../ui/components.tsx";
import { OverlayCard } from "../overlay/OverlayFrame.tsx";
import "./agents.css";
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

const STATUS_ICON = {
  queued: CircleDashed,
  running: Loader2,
  done: CheckCircle2,
  failed: AlertCircle,
} as const;

export function DelegationView(props: { state: DelegationState; now: number }) {
  const t = useT();
  const sum = totals(props.state);
  const [open, setOpen] = useState<string | null>(null);
  return (
    <div className="agents-view">
      <p className="agents-summary" role="status" data-testid="agents-totals">
        {t("manage.agents.totals", {
          workers: sum.workers,
          active: sum.active,
          tools: sum.tools,
          files: sum.files,
        })}
      </p>
      {props.state.batches.map((batch) => {
        const active = batch.workers.filter((w) => w.status === "running").length;
        return (
          <section className="agents-batch" key={batch.callId}>
            <h3 className="agents-batch-label">
              {t("agents.batch", { count: batch.workers.length })}
              {active > 0 && (
                <span className="agents-active">{t("agents.active", { count: active })}</span>
              )}
            </h3>
            <ul>
              {batch.workers.map((worker) => {
                const Icon = STATUS_ICON[worker.status];
                const key = `${batch.callId}/${worker.index}`;
                const expanded = open === key;
                return (
                  <li key={worker.index} className="agents-worker" data-status={worker.status}>
                    <button
                      type="button"
                      className="agents-worker-main"
                      aria-expanded={expanded}
                      onClick={() => setOpen(expanded ? null : key)}
                    >
                      <Icon aria-hidden="true" className="agents-glyph" size={14} />
                      <span className="agents-worker-text">
                        <span className="agents-goal">{worker.goal}</span>
                        {worker.lastNote !== "" && (
                          <span className="agents-note">{worker.lastNote}</span>
                        )}
                      </span>
                      <span className="agents-elapsed">
                        {elapsed(worker.startedAt, worker.endedAt, props.now)}
                      </span>
                    </button>
                    {expanded && (
                      <div className="agents-details">
                        <p>
                          {t(`manage.agents.status.${worker.status}`)} ·{" "}
                          {t(
                            worker.tools === 1 ? "manage.agents.toolsOne" : "manage.agents.tools",
                            {
                              count: worker.tools,
                            },
                          )}
                        </p>
                        {worker.files.length > 0 && (
                          <>
                            <p className="agents-mini">{t("agents.files")}</p>
                            {worker.files.slice(0, 12).map((file) => (
                              <p key={file}>{file}</p>
                            ))}
                          </>
                        )}
                      </div>
                    )}
                  </li>
                );
              })}
            </ul>
          </section>
        );
      })}
    </div>
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
  const empty = sessionId === null || state.batches.length === 0;
  return (
    <OverlayCard
      id="agents"
      title={t("nav.agents")}
      closeLabel={t("common.close")}
      onClose={props.onClose}
    >
      {empty ? (
        <div className="agents-empty">
          <EmptyState
            look="panel"
            icon={<Bot aria-hidden="true" />}
            title={t("agents.none")}
            description={sessionId === null ? t("manage.agents.noSession") : t("agents.noneBody")}
          />
        </div>
      ) : (
        <div className="desk-list-detail">
          <header className="desk-list-detail-head">
            <div className="desk-list-detail-titles">
              <h2>{t("nav.agents")}</h2>
              <p className="desk-list-detail-subtitle">{t("agents.subtitle")}</p>
            </div>
          </header>
          <DelegationView state={state} now={now} />
        </div>
      )}
    </OverlayCard>
  );
}
