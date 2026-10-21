// The Command center (P24-28): Sessions (counts, recent sessions, delete the empty ones), System
// (host stats and component health), Usage (tokens, cost and sessions over 7/30/90 days),
// Maintenance (doctor, backup, prune checkpoints, prune old sessions — the bulk ones go through the
// P17-21 maintenance gate, so a running daemon answers 409 and the page says who holds it — and
// the session import from Claude Code / Codex), and a log viewer over the four log files (agent,
// errors, gateway from the profile; the desktop's own log from main) with a level filter and a
// text filter.

import { useEffect, useState } from "react";
import type { IndusDesktopBridge } from "../../global.d.ts";
import { useT } from "../../i18n/index.tsx";
import { type RestLike, rest } from "../../settings/settingsApi.ts";
import { desktopBridge } from "../bridge.ts";
import { LoadState, PageFrame, useApi, useRestValue, when } from "../manage/common.tsx";

export const SECTIONS = ["sessions", "system", "usage", "maintenance", "logs"] as const;
export type Section = (typeof SECTIONS)[number];
export const LOG_SOURCES = ["agent", "errors", "gateway", "desktop"] as const;
export type LogSource = (typeof LOG_SOURCES)[number];
export const LOG_LEVELS = ["ALL", "DEBUG", "INFO", "WARNING", "ERROR"] as const;
export type LogLevel = (typeof LOG_LEVELS)[number];
const LEVEL_RANK: Readonly<Record<string, number>> = {
  DEBUG: 10,
  INFO: 20,
  WARNING: 30,
  ERROR: 40,
  CRITICAL: 50,
};

/** The desktop log has no server-side filter: keep lines at or above `level` (others follow the
 * line above them, like a traceback). */
export function filterByLevel(lines: readonly string[], level: LogLevel): string[] {
  if (level === "ALL") return [...lines];
  const floor = LEVEL_RANK[level] ?? 0;
  const out: string[] = [];
  let keep = false;
  for (const line of lines) {
    const found = /\b(DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL)\b/.exec(line)?.[1];
    if (found !== undefined)
      keep = (LEVEL_RANK[found === "WARN" ? "WARNING" : found] ?? 0) >= floor;
    if (keep) out.push(line);
  }
  return out;
}

function SessionsSection(props: { api: RestLike | null; profile: string | null }) {
  const t = useT();
  const stats = useRestValue<{ total: number; active?: number; empty?: number; messages?: number }>(
    () => rest(props.api, "GET", "/api/sessions/stats", { profile: props.profile }),
    [props.api, props.profile],
  );
  const recent = useRestValue<{
    sessions: Array<{ id: string; title: string | null; last_active?: number | null }>;
  }>(
    () => rest(props.api, "GET", "/api/sessions?limit=10", { profile: props.profile }),
    [props.api, props.profile],
  );
  const [note, setNote] = useState("");
  const dropEmpty = async () => {
    const answer = await rest<{ deleted?: number }>(props.api, "DELETE", "/api/sessions/empty", {
      profile: props.profile,
    });
    setNote(
      answer.ok ? t("manage.cc.emptyDeleted", { count: answer.data.deleted ?? 0 }) : answer.message,
    );
    stats.reload();
    recent.reload();
  };
  return (
    <div className="manage-body">
      <LoadState value={stats}>
        {(data) => (
          <p>
            {t("manage.cc.sessionStats", {
              total: data.total,
              active: data.active ?? 0,
              empty: data.empty ?? 0,
            })}
          </p>
        )}
      </LoadState>
      <button type="button" className="button button-secondary" onClick={() => void dropEmpty()}>
        {t("manage.cc.deleteEmpty")}
      </button>
      {note !== "" && <p className="manage-muted">{note}</p>}
      <LoadState value={recent}>
        {(data) => (
          <ul>
            {data.sessions.map((s) => (
              <li key={s.id}>
                <a href={`#/${encodeURIComponent(s.id)}`}>{s.title ?? s.id}</a>{" "}
                <span className="manage-muted">{when(s.last_active ?? null)}</span>
              </li>
            ))}
          </ul>
        )}
      </LoadState>
    </div>
  );
}

function SystemSection(props: { api: RestLike | null; profile: string | null }) {
  const t = useT();
  const stats = useRestValue<Record<string, unknown>>(
    () => rest(props.api, "GET", "/api/system/stats", { profile: props.profile }),
    [props.api, props.profile],
  );
  const status = useRestValue<Record<string, unknown>>(
    () => rest(props.api, "GET", "/api/status", { profile: props.profile }),
    [props.api, props.profile],
  );
  const rows = (data: Record<string, unknown>) =>
    Object.entries(data).filter(
      ([, v]) => typeof v === "string" || typeof v === "number" || typeof v === "boolean",
    );
  return (
    <div className="manage-grid">
      <div className="manage-card">
        <h3>{t("manage.cc.host")}</h3>
        <LoadState value={stats}>
          {(data) => (
            <table className="manage-table">
              <tbody>
                {rows(data).map(([key, value]) => (
                  <tr key={key}>
                    <th>{key}</th>
                    <td>{String(value)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </LoadState>
      </div>
      <div className="manage-card">
        <h3>{t("manage.cc.health")}</h3>
        <LoadState value={status}>
          {(data) => (
            <table className="manage-table">
              <tbody>
                {rows(data).map(([key, value]) => (
                  <tr key={key}>
                    <th>{key}</th>
                    <td>{String(value)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </LoadState>
      </div>
    </div>
  );
}

function UsageSection(props: { api: RestLike | null; profile: string | null }) {
  const t = useT();
  const [days, setDays] = useState(7);
  const usage = useRestValue<{
    totals: { input_tokens: number; output_tokens: number; cost: number; sessions: number };
  }>(
    () => rest(props.api, "GET", `/api/analytics/usage?days=${days}`, { profile: props.profile }),
    [props.api, props.profile, days],
  );
  return (
    <div className="manage-body">
      <fieldset className="manage-inline" aria-label={t("manage.cc.period")}>
        {[7, 30, 90].map((n) => (
          <button
            key={n}
            type="button"
            aria-pressed={days === n}
            className="button button-secondary"
            onClick={() => setDays(n)}
          >
            {t("manage.cc.days", { count: n })}
          </button>
        ))}
      </fieldset>
      <LoadState value={usage}>
        {(data) => (
          <p>
            {t("manage.cc.usage", {
              input: data.totals.input_tokens,
              output: data.totals.output_tokens,
              cost: data.totals.cost.toFixed(2),
              sessions: data.totals.sessions,
            })}
          </p>
        )}
      </LoadState>
    </div>
  );
}

interface Candidate {
  source: string;
  path: string;
  title: string;
  cwd: string | null;
  modified: number;
}

export function SessionImport(props: { api: RestLike | null; profile: string | null }) {
  const t = useT();
  const [source, setSource] = useState<"" | "claude-code" | "codex">("");
  const candidates = useRestValue<{ candidates: Candidate[] }>(
    () =>
      rest(
        props.api,
        "GET",
        `/api/session-import/candidates${source === "" ? "" : `?source=${source}`}`,
        {
          profile: props.profile,
        },
      ),
    [props.api, props.profile, source],
  );
  const [picked, setPicked] = useState<Candidate | null>(null);
  const [preview, setPreview] = useState<{
    title: string;
    messages: Array<{ role: string; content: string; tool?: string }>;
    skipped: number;
  } | null>(null);
  const [note, setNote] = useState("");
  const show = async (row: Candidate) => {
    setPicked(row);
    setPreview(null);
    const answer = await rest<NonNullable<typeof preview>>(
      props.api,
      "POST",
      "/api/session-import/preview",
      {
        profile: props.profile,
        body: { source: row.source, path: row.path },
      },
    );
    if (answer.ok) {
      setPreview(answer.data);
      setNote("");
    } else setNote(answer.message);
  };
  const doImport = async () => {
    if (picked === null) return;
    const answer = await rest<{ session_id: string; message_count: number }>(
      props.api,
      "POST",
      "/api/session-import/import",
      {
        profile: props.profile,
        body: { source: picked.source, path: picked.path },
      },
    );
    setNote(
      answer.ok ? t("manage.import.done", { count: answer.data.message_count }) : answer.message,
    );
  };
  return (
    <div className="manage-card" data-testid="session-import">
      <h3>{t("manage.import.title")}</h3>
      <div className="manage-inline">
        {(["", "claude-code", "codex"] as const).map((kind) => (
          <button
            key={kind || "all"}
            type="button"
            className="button button-secondary"
            aria-pressed={source === kind}
            onClick={() => setSource(kind)}
          >
            {t(
              kind === ""
                ? "manage.import.all"
                : kind === "codex"
                  ? "manage.import.codex"
                  : "manage.import.claude",
            )}
          </button>
        ))}
      </div>
      <LoadState value={candidates}>
        {(data) =>
          data.candidates.length === 0 ? (
            <p className="manage-muted">{t("manage.import.none")}</p>
          ) : (
            <ul>
              {data.candidates.map((row) => (
                <li key={row.path}>
                  <button
                    type="button"
                    className="button button-secondary"
                    onClick={() => void show(row)}
                  >
                    {row.title}
                  </button>{" "}
                  <span className="manage-muted">{row.cwd ?? ""}</span>
                </li>
              ))}
            </ul>
          )
        }
      </LoadState>
      {preview !== null && (
        <div className="manage-body" data-testid="import-preview">
          <p>
            {t("manage.import.preview", { title: preview.title, count: preview.messages.length })}
          </p>
          <ol>
            {preview.messages.slice(0, 50).map((m, i) => (
              // biome-ignore lint/suspicious/noArrayIndexKey: a fixed preview list
              <li key={i}>
                <strong>{m.role}</strong>
                {m.tool !== undefined && <span className="manage-muted"> [{m.tool}]</span>}:{" "}
                {m.content.slice(0, 200)}
              </li>
            ))}
          </ol>
          <button type="button" className="button button-primary" onClick={() => void doImport()}>
            {t("manage.import.import")}
          </button>
        </div>
      )}
      {note !== "" && (
        <p className="manage-muted" role="status">
          {note}
        </p>
      )}
    </div>
  );
}

function MaintenanceSection(props: { api: RestLike | null; profile: string | null }) {
  const t = useT();
  const [note, setNote] = useState("");
  const [days, setDays] = useState(90);
  const run = async (path: string, body?: unknown) => {
    const answer = await rest<{ action?: string; deleted?: number; would_delete?: number }>(
      props.api,
      "POST",
      path,
      {
        profile: props.profile,
        ...(body === undefined ? {} : { body }),
      },
    );
    if (answer.ok) setNote(t("manage.cc.started"));
    else if (answer.status === 409) setNote(t("manage.cc.busy", { reason: answer.message }));
    else setNote(answer.message);
  };
  return (
    <div className="manage-body">
      <div className="manage-inline">
        <button
          type="button"
          className="button button-secondary"
          onClick={() => void run("/api/ops/doctor")}
        >
          {t("manage.cc.doctor")}
        </button>
        <button
          type="button"
          className="button button-secondary"
          onClick={() => void run("/api/ops/backup")}
        >
          {t("manage.cc.backup")}
        </button>
        <button
          type="button"
          className="button button-secondary"
          onClick={() => void run("/api/ops/checkpoints/prune")}
        >
          {t("manage.cc.pruneCheckpoints")}
        </button>
      </div>
      <div className="manage-inline">
        <label className="manage-field">
          {t("manage.cc.olderThan")}
          <input
            type="number"
            min={1}
            value={days}
            onChange={(e) => setDays(Number(e.target.value) || 1)}
          />
        </label>
        <button
          type="button"
          className="button button-secondary"
          onClick={() => void run("/api/sessions/prune", { older_than_days: days, dry_run: false })}
        >
          {t("manage.cc.pruneSessions")}
        </button>
      </div>
      {note !== "" && (
        <p className="manage-muted" role="status">
          {note}
        </p>
      )}
      <SessionImport api={props.api} profile={props.profile} />
    </div>
  );
}

export function LogViewer(props: {
  api: RestLike | null;
  profile: string | null;
  bridge?: Pick<IndusDesktopBridge, "logs"> | null;
}) {
  const t = useT();
  const [source, setSource] = useState<LogSource>("agent");
  const [level, setLevel] = useState<LogLevel>("ALL");
  const [query, setQuery] = useState("");
  const [lines, setLines] = useState<string[]>([]);
  const [error, setError] = useState("");
  const bridge = props.bridge === undefined ? desktopBridge() : props.bridge;
  useEffect(() => {
    let live = true;
    const load = async () => {
      if (source === "desktop") {
        if (bridge === null) {
          setError(t("manage.cc.noDesktopLog"));
          return;
        }
        try {
          const answer = await bridge.logs.recent({ limit: 500 });
          if (live) {
            setLines(filterByLevel(answer.lines, level));
            setError("");
          }
        } catch {
          if (live) setError(t("manage.failed"));
        }
        return;
      }
      const answer = await rest<{ lines: string[] }>(
        props.api,
        "GET",
        `/api/logs?file=${source}&lines=500${level === "ALL" ? "" : `&level=${level}`}`,
        { profile: props.profile },
      );
      if (!live) return;
      if (answer.ok) {
        setLines(answer.data.lines);
        setError("");
      } else setError(answer.message);
    };
    void load();
    return () => {
      live = false;
    };
  }, [source, level, props.api, props.profile, bridge, t]);
  const shown =
    query.trim() === ""
      ? lines
      : lines.filter((l) => l.toLowerCase().includes(query.trim().toLowerCase()));
  return (
    <div className="manage-body">
      <div className="manage-inline">
        <label className="manage-field">
          {t("manage.cc.logFile")}
          <select value={source} onChange={(e) => setSource(e.target.value as LogSource)}>
            {LOG_SOURCES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </label>
        <label className="manage-field">
          {t("manage.cc.logLevel")}
          <select value={level} onChange={(e) => setLevel(e.target.value as LogLevel)}>
            {LOG_LEVELS.map((l) => (
              <option key={l} value={l}>
                {l}
              </option>
            ))}
          </select>
        </label>
        <label className="manage-field">
          {t("manage.cc.logSearch")}
          <input value={query} onChange={(e) => setQuery(e.target.value)} />
        </label>
      </div>
      {error !== "" && <p className="manage-error">{error}</p>}
      <pre className="manage-log" data-testid="log-lines">
        {shown.join("\n")}
      </pre>
    </div>
  );
}

export function CommandCenter(props: {
  api?: RestLike | null;
  profile: string | null;
  bridge?: Pick<IndusDesktopBridge, "logs"> | null;
  initial?: Section;
}) {
  const t = useT();
  const api = useApi(props.api);
  const [section, setSection] = useState<Section>(props.initial ?? "sessions");
  return (
    <PageFrame title={t("nav.command-center")} id="command-center">
      <div className="manage-tabs" role="tablist">
        {SECTIONS.map((s) => (
          <button
            key={s}
            type="button"
            role="tab"
            aria-selected={section === s}
            className="button button-secondary"
            onClick={() => setSection(s)}
          >
            {t(`manage.cc.section.${s}`)}
          </button>
        ))}
      </div>
      {section === "sessions" && <SessionsSection api={api} profile={props.profile} />}
      {section === "system" && <SystemSection api={api} profile={props.profile} />}
      {section === "usage" && <UsageSection api={api} profile={props.profile} />}
      {section === "maintenance" && <MaintenanceSection api={api} profile={props.profile} />}
      {section === "logs" && (
        <LogViewer
          api={api}
          profile={props.profile}
          {...(props.bridge === undefined ? {} : { bridge: props.bridge })}
        />
      )}
    </PageFrame>
  );
}
