// The Command center (P24-28; W52-l look): a full-window overlay with a 208px rail —
// Sessions (every session, newest activity first: open, pin, delete), System (the gateway's state,
// restart and update as tailed actions, and the recent log lines of the four log files: agent,
// errors, gateway from the profile; the desktop's own log from main), Usage (sessions, API calls and
// tokens over 7/30/90 days as tiles, a daily bar chart and the top models — W58-J1-1, from what
// the store recorded) and Maintenance (doctor, backup, the session
// clean-ups — the bulk ones go through the P17-21 maintenance gate, so a running daemon answers 409
// and the page says who holds it —, memory files, and the way into the session import overlay).
// Data our backend does not have (per-skill usage per period, a security
// audit, debug share, the skill curator, memory reset, session export) shows as a disabled or
// empty placeholder, never an invented call.

import {
  Activity,
  BarChart3,
  Bookmark,
  BookmarkCheck,
  Download,
  MessageCircle,
  Trash2,
  Wrench,
} from "lucide-react";
import { type ReactNode, useEffect, useRef, useState } from "react";
import type { IndusDesktopBridge } from "../../global.d.ts";
import { useT } from "../../i18n/index.tsx";
import { type RestAnswer, type RestLike, rest } from "../../settings/settingsApi.ts";
import { Button, IconButton, SmallDialog } from "../../ui/components.tsx";
import { desktopBridge } from "../bridge.ts";
import { copyText } from "../clipboard.ts";
import {
  CapsLabel,
  InlineError,
  LogTail,
  PanelPill,
  QuietSearch,
  StatTile,
  TextAction,
  TextTabs,
} from "../kit.tsx";
import { Loader } from "../Loader.tsx";
import { useApi, useRestValue } from "../manage/common.tsx";
import {
  Breadcrumb,
  NavEntry,
  NavFrame,
  OverlayCard,
  Segmented,
} from "../overlay/OverlayFrame.tsx";
import "./commandCenter.css";

export const SECTIONS = ["sessions", "system", "usage", "maintenance"] as const;
export type Section = (typeof SECTIONS)[number];
export const LOG_SOURCES = ["agent", "errors", "gateway", "desktop"] as const;
export type LogSource = (typeof LOG_SOURCES)[number];
/** W52-l-2: the reference's all/info/warning/error, plus our debug as a fifth tab. */
export const LOG_LEVELS = ["ALL", "DEBUG", "INFO", "WARNING", "ERROR"] as const;
export type LogLevel = (typeof LOG_LEVELS)[number];
export const LOG_LINES = 200;
export const SESSION_PAGE = 50;
const LEVEL_RANK: Readonly<Record<string, number>> = {
  DEBUG: 10,
  INFO: 20,
  WARNING: 30,
  ERROR: 40,
  CRITICAL: 50,
};
const SECTION_ICONS: Readonly<Record<Section, typeof Activity>> = {
  sessions: MessageCircle,
  system: Activity,
  usage: BarChart3,
  maintenance: Wrench,
};

export function isSection(value: string | null): value is Section {
  return value !== null && (SECTIONS as readonly string[]).includes(value);
}

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

/** "Oct 24, 2026, 1:06 PM" style: the locale's medium date and short time. */
export function dateTime(value: number | string | null | undefined): string {
  if (value === null || value === undefined || value === "") return "";
  const ms = typeof value === "number" ? (value > 1e12 ? value : value * 1000) : Date.parse(value);
  if (!Number.isFinite(ms)) return "";
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(ms);
}

/** 1.2K, 3.4M. */
export function compact(value: number): string {
  return new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 }).format(
    value,
  );
}

// -- a tailed action (restart, update, doctor, backup, prune) ------------------------------------

export interface ActionRun {
  readonly label: string;
  readonly name: string | null;
  readonly status: "running" | "done" | "failed";
  readonly lines: readonly string[];
  readonly note: string;
}

export const POLL_MS = 1200;
const MAX_POLLS = 150;

/** Start an action with `start`; when the answer names an action, tail
 * `/api/actions/{name}/status` every 1.2s until it stops. */
export function useActionRun(api: RestLike | null, profile: string | null) {
  const [run, setRun] = useState<ActionRun | null>(null);
  const live = useRef(true);
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
    };
  }, []);
  const start = async (
    label: string,
    call: () => Promise<RestAnswer<{ action?: string; archive?: string }>>,
    busy: (answer: RestAnswer<unknown> & { ok: false }) => string,
  ): Promise<RestAnswer<{ action?: string; archive?: string }>> => {
    setRun({ label, name: null, status: "running", lines: [], note: "" });
    const answer = await call();
    if (!live.current) return answer;
    if (!answer.ok) {
      setRun({ label, name: null, status: "failed", lines: [], note: busy(answer) });
      return answer;
    }
    const name = answer.data.action ?? null;
    const archive = answer.data.archive ?? "";
    if (name === null) {
      setRun({ label, name, status: "done", lines: [], note: archive });
      return answer;
    }
    setRun({ label, name, status: "running", lines: [], note: archive });
    for (let poll = 0; poll < MAX_POLLS && live.current; poll += 1) {
      await new Promise((resolve) => setTimeout(resolve, POLL_MS));
      if (!live.current) break;
      const status = await rest<{ state?: string; running?: boolean; lines?: string[] }>(
        api,
        "GET",
        `/api/actions/${encodeURIComponent(name)}/status`,
        { profile },
      );
      if (!live.current) break;
      if (!status.ok) {
        setRun({ label, name, status: "failed", lines: [], note: status.message });
        break;
      }
      const lines = status.data.lines ?? [];
      const running = status.data.running === true;
      const failed = status.data.state === "failed";
      setRun({
        label,
        name,
        status: running ? "running" : failed ? "failed" : "done",
        lines,
        note: archive,
      });
      if (!running) break;
    }
    return answer;
  };
  return { run, start, busy: run?.status === "running" };
}

// -- Sessions -------------------------------------------------------------------------------------

interface SessionRow {
  id: string;
  title: string | null;
  preview?: string | null;
  last_active?: number | null;
  started_at?: number | null;
  pinned?: boolean;
}

function SessionsSection(props: {
  api: RestLike | null;
  profile: string | null;
  query: string;
  onOpenSession: (id: string) => void;
}) {
  const t = useT();
  const [rows, setRows] = useState<SessionRow[]>([]);
  const [total, setTotal] = useState(0);
  const [state, setState] = useState<"loading" | "ok" | "error">("loading");
  const [error, setError] = useState("");
  const [deleting, setDeleting] = useState<SessionRow | null>(null);
  const [busy, setBusy] = useState(false);
  const [filter, setFilter] = useState(props.query);
  useEffect(() => {
    const timer = setTimeout(() => setFilter(props.query), 180);
    return () => clearTimeout(timer);
  }, [props.query]);
  const load = async (offset: number) => {
    const answer = await rest<{ sessions: SessionRow[]; total?: number }>(
      props.api,
      "GET",
      `/api/sessions?order=recent&limit=${SESSION_PAGE}&offset=${offset}`,
      { profile: props.profile },
    );
    if (!answer.ok) {
      setState("error");
      setError(answer.message);
      return;
    }
    setRows((current) =>
      offset === 0 ? answer.data.sessions : [...current, ...answer.data.sessions],
    );
    setTotal(answer.data.total ?? answer.data.sessions.length);
    setState("ok");
  };
  // biome-ignore lint/correctness/useExhaustiveDependencies: reload when the connection changes
  useEffect(() => {
    void load(0);
  }, [props.api, props.profile]);
  const needle = filter.trim().toLowerCase();
  const shown =
    needle === ""
      ? rows
      : rows.filter(
          (row) =>
            (row.title ?? "").toLowerCase().includes(needle) ||
            row.id.toLowerCase().includes(needle),
        );
  const pin = async (row: SessionRow) => {
    const pinned = row.pinned !== true;
    const answer = await rest(props.api, "PATCH", `/api/sessions/${encodeURIComponent(row.id)}`, {
      profile: props.profile,
      body: { pinned },
    });
    if (answer.ok) setRows((all) => all.map((r) => (r.id === row.id ? { ...r, pinned } : r)));
    else setError(answer.message);
  };
  const remove = async () => {
    if (deleting === null) return;
    setBusy(true);
    const answer = await rest(
      props.api,
      "DELETE",
      `/api/sessions/${encodeURIComponent(deleting.id)}`,
      { profile: props.profile },
    );
    setBusy(false);
    if (answer.ok) {
      setRows((all) => all.filter((r) => r.id !== deleting.id));
      setTotal((n) => Math.max(0, n - 1));
      setDeleting(null);
    } else setError(answer.message);
  };
  if (state === "loading") return <Loader />;
  return (
    <div className="cc-sessions">
      {error !== "" && <InlineError>{error}</InlineError>}
      {shown.length === 0 ? (
        <p className="cc-empty-line">
          {needle === "" ? t("cc.sessions.none") : t("cc.sessions.noMatch")}
        </p>
      ) : (
        <ul className="cc-session-list" aria-label={t("cc.section.sessions")}>
          {shown.map((row) => {
            const title = row.title || row.preview || t("cc.sessions.untitled");
            return (
              <li key={row.id} className="cc-session" data-session={row.id}>
                <button
                  type="button"
                  className="cc-session-main"
                  onClick={() => props.onOpenSession(row.id)}
                >
                  <span className="cc-session-title">{title}</span>
                  <span className="cc-session-date">
                    {dateTime(row.last_active ?? row.started_at ?? null)}
                  </span>
                </button>
                <span className="cc-session-actions">
                  <IconButton
                    label={
                      row.pinned === true
                        ? t("cc.sessions.unpin", { title })
                        : t("cc.sessions.pin", { title })
                    }
                    title={
                      row.pinned === true ? t("cc.sessions.unpinTip") : t("cc.sessions.pinTip")
                    }
                    icon={
                      row.pinned === true ? (
                        <BookmarkCheck aria-hidden="true" size={14} />
                      ) : (
                        <Bookmark aria-hidden="true" size={14} />
                      )
                    }
                    onClick={() => void pin(row)}
                  />
                  <IconButton
                    label={t("cc.sessions.export", { title })}
                    title={t("cc.notYet")}
                    icon={<Download aria-hidden="true" size={14} />}
                    disabled
                  />
                  <IconButton
                    label={t("cc.sessions.delete", { title })}
                    title={t("cc.sessions.deleteTip")}
                    className="cc-danger-icon"
                    icon={<Trash2 aria-hidden="true" size={14} />}
                    onClick={() => setDeleting(row)}
                  />
                </span>
              </li>
            );
          })}
        </ul>
      )}
      {rows.length < total && (
        <div className="cc-more">
          <Button variant="ghost" size="sm" onClick={() => void load(rows.length)}>
            {t("cc.sessions.more")}
          </Button>
        </div>
      )}
      {deleting !== null && (
        <SmallDialog
          role="alertdialog"
          title={t("cc.sessions.deleteTitle")}
          description={t("cc.sessions.deleteBody", {
            title: deleting.title || t("cc.sessions.untitled"),
          })}
          onClose={() => setDeleting(null)}
          actions={
            <>
              <Button variant="outline" onClick={() => setDeleting(null)}>
                {t("manage.cancel")}
              </Button>
              <Button variant="danger" disabled={busy} onClick={() => void remove()}>
                {busy ? t("cc.sessions.deleting") : t("manage.delete")}
              </Button>
            </>
          }
        />
      )}
    </div>
  );
}

// -- System ---------------------------------------------------------------------------------------

function runCaption(t: ReturnType<typeof useT>, run: ActionRun): string {
  return t("cc.run.caption", { action: run.label, status: t(`cc.run.${run.status}`) });
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
          setLines([]);
          setError(t("manage.cc.noDesktopLog"));
          return;
        }
        try {
          const answer = await bridge.logs.recent({ limit: LOG_LINES });
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
        `/api/logs?file=${source}&lines=${LOG_LINES}${level === "ALL" ? "" : `&level=${level}`}`,
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
  const needle = query.trim().toLowerCase();
  const shown = needle === "" ? lines : lines.filter((l) => l.toLowerCase().includes(needle));
  return (
    <div className="cc-logs">
      <CapsLabel
        aside={
          <>
            {error !== "" && (
              <span className="cc-log-error" role="alert">
                {error}
              </span>
            )}
            <TextTabs
              label={t("manage.cc.logFile")}
              value={source}
              options={LOG_SOURCES.map((s) => ({ value: s, label: s }))}
              onChange={setSource}
            />
            <TextTabs
              label={t("manage.cc.logLevel")}
              value={level}
              options={LOG_LEVELS.map((l) => ({ value: l, label: l.toLowerCase() }))}
              onChange={setLevel}
            />
            <QuietSearch
              label={t("cc.logs.search")}
              placeholder={t("cc.logs.searchPlaceholder")}
              value={query}
              onChange={setQuery}
              width={176}
            />
          </>
        }
      >
        {t("cc.logs.title")}
      </CapsLabel>
      <LogTail
        lines={shown}
        emptyText={t("cc.logs.empty")}
        testId="log-lines"
        labels={{ top: t("cc.logs.top"), bottom: t("cc.logs.bottom"), copy: t("cc.logs.copy") }}
        onCopy={(text) => void copyText(text)}
      />
    </div>
  );
}

function SystemSection(props: {
  api: RestLike | null;
  profile: string | null;
  bridge?: Pick<IndusDesktopBridge, "logs"> | null;
}) {
  const t = useT();
  const daemon = useRestValue<{ running?: boolean; state?: string }>(
    () => rest(props.api, "GET", "/api/daemon", { profile: props.profile }),
    [props.api, props.profile],
  );
  const status = useRestValue<{ version?: string; active_sessions?: number }>(
    () => rest(props.api, "GET", "/api/status", { profile: props.profile }),
    [props.api, props.profile],
  );
  const action = useActionRun(props.api, props.profile);
  const restart = () =>
    void action
      .start(
        t("cc.system.restart"),
        () => rest(props.api, "POST", "/api/daemon/restart", { profile: props.profile }),
        (answer) => answer.message,
      )
      .then(() => daemon.reload());
  const update = () =>
    void action.start(
      t("cc.system.update"),
      () => rest(props.api, "POST", "/api/system/update", { profile: props.profile }),
      (answer) => answer.message,
    );
  const running = daemon.state === "ok" && daemon.data.running === true;
  return (
    <div className="cc-system">
      {daemon.state === "loading" ? (
        <Loader />
      ) : (
        <div className="cc-status">
          <div className="cc-status-line">
            <span className="cc-status-dot" data-running={running ? "true" : "false"} />
            <span className="cc-status-label">
              {running ? t("cc.system.running") : t("cc.system.stopped")}
            </span>
            <span className="cc-status-actions">
              <TextAction onClick={restart} disabled={action.busy}>
                {t("cc.system.restart")}
              </TextAction>
              <TextAction strong onClick={update} disabled={action.busy}>
                {t("cc.system.update")}
              </TextAction>
            </span>
          </div>
          <p className="cc-status-caption">
            {status.state === "ok"
              ? t("cc.system.caption", {
                  version: status.data.version ?? "—",
                  count: status.data.active_sessions ?? 0,
                })
              : status.state === "error"
                ? status.message
                : ""}
          </p>
          {daemon.state === "error" && <InlineError>{daemon.message}</InlineError>}
          {action.run !== null && (
            <p className="cc-status-caption" role="status">
              {runCaption(t, action.run)}
              {action.run.note !== "" && action.run.status === "failed"
                ? ` — ${action.run.note}`
                : ""}
            </p>
          )}
        </div>
      )}
      <LogViewer
        api={props.api}
        profile={props.profile}
        {...(props.bridge === undefined ? {} : { bridge: props.bridge })}
      />
    </div>
  );
}

// -- Usage ----------------------------------------------------------------------------------------

interface UsageDay {
  day: string;
  input_tokens: number;
  output_tokens: number;
  cost?: number;
  sessions?: number;
}

interface ModelUsageRow {
  model: string;
  input_tokens: number;
  output_tokens: number;
  api_calls: number;
  cost: number;
}

interface UsageAnswer {
  daily?: UsageDay[];
  totals: {
    input_tokens: number;
    output_tokens: number;
    cost: number;
    sessions: number;
    /** Absent from a backend older than W58-J1-1. */
    api_calls?: number;
  };
  by_model?: ModelUsageRow[];
}

/** How many models the Top models column lists. */
export const TOP_MODELS = 5;

function TopModels(props: { rows: readonly ModelUsageRow[] | undefined }) {
  const t = useT();
  if (props.rows === undefined)
    return <p className="cc-empty-line cc-left">{t("cc.usage.noModels")}</p>;
  if (props.rows.length === 0)
    return <p className="cc-empty-line cc-left">{t("cc.usage.noModelsInPeriod")}</p>;
  return (
    <ul className="cc-top-list" aria-label={t("cc.usage.topModels")}>
      {props.rows.slice(0, TOP_MODELS).map((row) => (
        <li
          key={row.model}
          title={t("cc.usage.modelTip", {
            input: compact(row.input_tokens),
            output: compact(row.output_tokens),
            calls: compact(row.api_calls),
            cost: row.cost.toFixed(2),
          })}
        >
          <span className="cc-top-name">{row.model}</span>
          <span className="cc-top-value">{compact(row.input_tokens + row.output_tokens)}</span>
        </li>
      ))}
    </ul>
  );
}

export const PERIODS = ["7", "30", "90"] as const;
export type Period = (typeof PERIODS)[number];

function DailyChart(props: { days: readonly UsageDay[] }) {
  const t = useT();
  const max = Math.max(1, ...props.days.map((d) => d.input_tokens + d.output_tokens));
  if (props.days.length === 0) {
    return <p className="cc-chart-empty">{t("cc.usage.noActivity")}</p>;
  }
  const first = props.days[0]?.day ?? "";
  const last = props.days[props.days.length - 1]?.day ?? "";
  return (
    <>
      <div className="cc-chart" role="img" aria-label={t("cc.usage.daily")}>
        {props.days.map((d) => (
          <div
            key={d.day}
            className="cc-bar"
            title={t("cc.usage.barTip", {
              day: d.day,
              input: compact(d.input_tokens),
              output: compact(d.output_tokens),
            })}
          >
            <span className="cc-bar-in" style={{ height: `${(d.input_tokens / max) * 100}%` }} />
            <span className="cc-bar-out" style={{ height: `${(d.output_tokens / max) * 100}%` }} />
          </div>
        ))}
      </div>
      <div className="cc-chart-ends">
        <span>{first}</span>
        <span>{last}</span>
      </div>
    </>
  );
}

function UsageSection(props: { api: RestLike | null; profile: string | null; days: Period }) {
  const t = useT();
  const usage = useRestValue<UsageAnswer>(
    () =>
      rest(props.api, "GET", `/api/analytics/usage?days=${props.days}`, { profile: props.profile }),
    [props.api, props.profile, props.days],
  );
  if (usage.state === "loading") return <Loader />;
  if (usage.state === "error") {
    return (
      <div className="cc-center">
        <p className="cc-empty-line">
          {usage.code === "not-connected" ? t("manage.notConnected") : usage.message}
        </p>
        <TextAction strong onClick={usage.reload}>
          {t("manage.retry")}
        </TextAction>
      </div>
    );
  }
  const totals = usage.data.totals;
  return (
    <div className="cc-usage">
      <div className="cc-tiles">
        <StatTile label={t("cc.usage.sessions")} value={compact(totals.sessions)} />
        {totals.api_calls === undefined ? (
          <StatTile label={t("cc.usage.apiCalls")} value="—" hint={t("cc.usage.apiCallsHint")} />
        ) : (
          <StatTile label={t("cc.usage.apiCalls")} value={compact(totals.api_calls)} />
        )}
        <StatTile
          label={t("cc.usage.tokens")}
          value={`${compact(totals.input_tokens)} / ${compact(totals.output_tokens)}`}
          hint={t("cc.usage.cost", { cost: totals.cost.toFixed(2) })}
        />
      </div>
      <section className="cc-usage-block">
        <CapsLabel
          aside={
            <span className="cc-legend">
              <span className="cc-legend-in" aria-hidden="true" />
              {t("cc.usage.input")}
              <span className="cc-legend-out" aria-hidden="true" />
              {t("cc.usage.output")}
            </span>
          }
        >
          {t("cc.usage.daily")}
        </CapsLabel>
        <DailyChart days={usage.data.daily ?? []} />
      </section>
      <div className="cc-usage-cols">
        <section>
          <CapsLabel>{t("cc.usage.topModels")}</CapsLabel>
          <TopModels rows={usage.data.by_model} />
        </section>
        <section>
          <CapsLabel>{t("cc.usage.topSkills")}</CapsLabel>
          <p className="cc-empty-line cc-left">{t("cc.usage.noSkills")}</p>
        </section>
      </div>
    </div>
  );
}

// -- Maintenance ----------------------------------------------------------------------------------

function sizeText(t: ReturnType<typeof useT>, bytes: number | undefined): string {
  if (bytes === undefined || bytes <= 0) return t("cc.memory.empty");
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function MaintRow(props: {
  title: string;
  description?: string;
  aside?: string;
  pill?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="cc-maint-row">
      <div className="cc-maint-text">
        <p className="cc-maint-title">
          {props.title}
          {props.aside !== undefined && <span className="cc-maint-aside">{props.aside}</span>}
          {props.pill}
        </p>
        {props.description !== undefined && <p className="cc-maint-desc">{props.description}</p>}
      </div>
      <div className="cc-maint-actions">{props.children}</div>
    </div>
  );
}

function MaintenanceSection(props: {
  api: RestLike | null;
  profile: string | null;
  onOpenImport: () => void;
}) {
  const t = useT();
  const [days, setDays] = useState(90);
  const [note, setNote] = useState("");
  const stats = useRestValue<{ total: number; active?: number; empty?: number }>(
    () => rest(props.api, "GET", "/api/sessions/stats", { profile: props.profile }),
    [props.api, props.profile],
  );
  const memory = useRestValue<{ active?: string; builtin_files?: Record<string, number> }>(
    () => rest(props.api, "GET", "/api/memory", { profile: props.profile }),
    [props.api, props.profile],
  );
  const action = useActionRun(props.api, props.profile);
  const busyText = (answer: { status: number | null; message: string }) =>
    answer.status === 409 ? t("manage.cc.busy", { reason: answer.message }) : answer.message;
  const start = (label: string, method: "POST" | "DELETE", path: string, body?: unknown) =>
    void action
      .start(
        label,
        () =>
          rest(props.api, method, path, {
            profile: props.profile,
            ...(body === undefined ? {} : { body }),
          }),
        busyText,
      )
      .then((answer) => {
        if (answer.ok && method === "DELETE") {
          const deleted = (answer.data as { deleted?: number }).deleted ?? 0;
          setNote(t("manage.cc.emptyDeleted", { count: deleted }));
        }
        stats.reload();
      });
  const run = action.run;
  const files = memory.state === "ok" ? (memory.data.builtin_files ?? {}) : {};
  return (
    <div className="cc-maint">
      <section>
        <CapsLabel>{t("cc.maint.diagnostics")}</CapsLabel>
        <MaintRow title={t("manage.cc.doctor")} description={t("cc.maint.doctorBody")}>
          <TextAction
            strong
            disabled={action.busy}
            onClick={() => start(t("manage.cc.doctor"), "POST", "/api/ops/doctor")}
          >
            {t("manage.cc.doctor")}
          </TextAction>
        </MaintRow>
        <MaintRow title={t("cc.maint.audit")} description={t("cc.maint.auditBody")}>
          <TextAction strong disabled title={t("cc.notYet")} onClick={() => {}}>
            {t("cc.maint.auditAction")}
          </TextAction>
        </MaintRow>
        <MaintRow title={t("cc.maint.backup")} description={t("cc.maint.backupBody")}>
          <TextAction
            strong
            disabled={action.busy}
            onClick={() => start(t("cc.maint.backup"), "POST", "/api/ops/backup")}
          >
            {t("cc.maint.backupAction")}
          </TextAction>
        </MaintRow>
        <MaintRow title={t("cc.maint.share")} description={t("cc.maint.shareBody")}>
          <TextAction strong disabled title={t("cc.notYet")} onClick={() => {}}>
            {t("cc.maint.shareAction")}
          </TextAction>
        </MaintRow>
      </section>
      <section>
        <CapsLabel>{t("cc.maint.sessions")}</CapsLabel>
        <MaintRow
          title={t("manage.cc.deleteEmpty")}
          description={
            stats.state === "ok"
              ? t("manage.cc.sessionStats", {
                  total: stats.data.total,
                  active: stats.data.active ?? 0,
                  empty: stats.data.empty ?? 0,
                })
              : t("cc.maint.emptyBody")
          }
        >
          <TextAction
            strong
            disabled={action.busy}
            onClick={() =>
              start(t("manage.cc.deleteEmpty"), "DELETE", "/api/sessions/empty", undefined)
            }
          >
            {t("cc.maint.deleteAction")}
          </TextAction>
        </MaintRow>
        <MaintRow title={t("manage.cc.pruneSessions")} description={t("cc.maint.pruneBody")}>
          <input
            className="cc-days"
            type="number"
            min={1}
            aria-label={t("manage.cc.olderThan")}
            value={days}
            onChange={(e) => setDays(Number(e.target.value) || 1)}
          />
          <TextAction
            strong
            disabled={action.busy}
            onClick={() =>
              start(t("manage.cc.pruneSessions"), "POST", "/api/sessions/prune", {
                older_than_days: days,
                dry_run: false,
              })
            }
          >
            {t("manage.cc.pruneSessions")}
          </TextAction>
        </MaintRow>
        <MaintRow title={t("manage.cc.pruneCheckpoints")} description={t("cc.maint.checkBody")}>
          <TextAction
            strong
            disabled={action.busy}
            onClick={() =>
              start(t("manage.cc.pruneCheckpoints"), "POST", "/api/ops/checkpoints/prune")
            }
          >
            {t("cc.maint.pruneAction")}
          </TextAction>
        </MaintRow>
        <MaintRow title={t("cc.maint.import")} description={t("cc.maint.importBody")}>
          <TextAction strong onClick={props.onOpenImport}>
            {t("cc.maint.open")}
          </TextAction>
        </MaintRow>
      </section>
      {run !== null && (
        <section className="cc-run" aria-live="polite">
          {run.status === "failed" && run.note !== "" ? (
            <InlineError>{run.note}</InlineError>
          ) : (
            <p className="cc-status-caption" role="status">
              {runCaption(t, run)}
              {run.status === "done" && run.note !== "" ? ` — ${run.note}` : ""}
              {note !== "" && run.status === "done" ? ` — ${note}` : ""}
            </p>
          )}
          {run.lines.length > 0 && (
            <>
              <CapsLabel>{t("cc.maint.viewLog")}</CapsLabel>
              <LogTail
                compact
                lines={run.lines}
                emptyText={t("cc.logs.empty")}
                labels={{
                  top: t("cc.logs.top"),
                  bottom: t("cc.logs.bottom"),
                  copy: t("cc.logs.copy"),
                }}
              />
            </>
          )}
        </section>
      )}
      <section>
        <CapsLabel>{t("cc.maint.curator")}</CapsLabel>
        <MaintRow
          title={t("cc.maint.curatorTitle")}
          description={t("cc.maint.curatorBody")}
          pill={<PanelPill tone="muted">{t("cc.maint.unavailable")}</PanelPill>}
        >
          <TextAction disabled title={t("cc.notYet")} onClick={() => {}}>
            {t("cc.maint.pause")}
          </TextAction>
          <TextAction strong disabled title={t("cc.notYet")} onClick={() => {}}>
            {t("manage.cron.runNow")}
          </TextAction>
        </MaintRow>
      </section>
      <section>
        <CapsLabel>{t("cc.maint.memory")}</CapsLabel>
        <p className="cc-maint-caption">
          {t("cc.memory.caption", {
            provider:
              memory.state === "ok" && (memory.data.active ?? "") !== ""
                ? (memory.data.active as string)
                : t("cc.memory.builtin"),
          })}
        </p>
        <MaintRow title={t("cc.memory.agent")} aside={sizeText(t, files["MEMORY.md"])}>
          <TextAction tone="danger" disabled title={t("cc.notYet")} onClick={() => {}}>
            {t("cc.memory.reset")}
          </TextAction>
        </MaintRow>
        <MaintRow title={t("cc.memory.user")} aside={sizeText(t, files["USER.md"])}>
          <TextAction tone="danger" disabled title={t("cc.notYet")} onClick={() => {}}>
            {t("cc.memory.resetProfile")}
          </TextAction>
        </MaintRow>
      </section>
    </div>
  );
}

// -- the overlay ----------------------------------------------------------------------------------

export function CommandCenter(props: {
  api?: RestLike | null;
  profile: string | null;
  bridge?: Pick<IndusDesktopBridge, "logs"> | null;
  initial?: Section;
  /** The section shown, when the router holds it (`?section=`). */
  section?: Section;
  onSection?: (section: Section) => void;
  onClose?: () => void;
  onOpenSession?: (sessionId: string) => void;
  onOpenImport?: () => void;
}) {
  const t = useT();
  const api = useApi(props.api);
  const [own, setOwn] = useState<Section>(props.initial ?? "sessions");
  const section = props.section ?? own;
  const select = (next: Section) => {
    setOwn(next);
    props.onSection?.(next);
  };
  const [query, setQuery] = useState("");
  const [days, setDays] = useState<Period>("30");
  const crumbs = [
    { label: t("cc.title"), onSelect: () => select("sessions") },
    { label: t(`cc.section.${section}`) },
  ];
  let aside: ReactNode = null;
  if (section === "sessions") {
    aside = (
      <QuietSearch
        label={t("cc.sessions.search")}
        placeholder={t("cc.sessions.searchPlaceholder")}
        value={query}
        onChange={setQuery}
      />
    );
  } else if (section === "usage") {
    aside = (
      <Segmented
        label={t("manage.cc.period")}
        value={days}
        options={PERIODS.map((p) => ({ value: p, label: t("cc.usage.period", { days: p }) }))}
        onChange={setDays}
      />
    );
  }
  return (
    <OverlayCard
      id="command-center"
      title={t("cc.title")}
      closeLabel={t("common.close")}
      onClose={props.onClose ?? (() => {})}
      className="cc-card"
    >
      <NavFrame
        navLabel={t("cc.title")}
        nav={
          <ul>
            {SECTIONS.map((s) => {
              const Icon = SECTION_ICONS[s];
              return (
                <NavEntry
                  key={s}
                  icon={<Icon aria-hidden="true" />}
                  label={t(`cc.section.${s}`)}
                  current={section === s}
                  onSelect={() => select(s)}
                />
              );
            })}
          </ul>
        }
        header={
          <div className="cc-head">
            <Breadcrumb label={t("cc.breadcrumb")} crumbs={crumbs} />
            {aside !== null && <div className="cc-head-aside">{aside}</div>}
          </div>
        }
      >
        {section === "sessions" && (
          <SessionsSection
            api={api}
            profile={props.profile}
            query={query}
            onOpenSession={props.onOpenSession ?? (() => {})}
          />
        )}
        {section === "system" && (
          <SystemSection
            api={api}
            profile={props.profile}
            {...(props.bridge === undefined ? {} : { bridge: props.bridge })}
          />
        )}
        {section === "usage" && <UsageSection api={api} profile={props.profile} days={days} />}
        {section === "maintenance" && (
          <MaintenanceSection
            api={api}
            profile={props.profile}
            onOpenImport={props.onOpenImport ?? (() => {})}
          />
        )}
      </NavFrame>
    </OverlayCard>
  );
}
