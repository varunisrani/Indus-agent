// The shell's bottom status bar (P20-23): per-session yolo, context meter with a breakdown, cache
// hits and tokens/s over the last 10 model calls, workspace, model, open approvals, the turn timer,
// the backend version and a daemon menu with Reconnect. Items can be hidden from a customise menu;
// the choice is kept per device.

import type { SessionInfo, UsageNumbers } from "@indus/protocol";
import { CircleAlert } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import type { StorageLike } from "../chat/inflightJournal.ts";
import { errorMessage } from "../chat/rpcErrors.ts";
import { ContributionArea } from "../contrib/react.tsx";
import type { ChatConnection, SessionEvent } from "../gateway/connectionStore.ts";
import { useGatewayConnection, useOpenServerRequests } from "../gateway/react.tsx";
import type { OpenServerRequest } from "../gateway/serverRequests.ts";
import { type MessageKey, useT } from "../i18n/index.tsx";
import { requestOnboarding } from "../onboarding/requests.ts";
import { useProviderReady } from "../onboarding/useProviderReady.ts";
import type { RestLike } from "../settings/settingsApi.ts";

export const STATUS_ITEMS = [
  "yolo",
  "context",
  "cache",
  "speed",
  "workspace",
  "model",
  "approvals",
  "timer",
  "version",
  "daemon",
] as const;
export type StatusItem = (typeof STATUS_ITEMS)[number];

/** Shown until the person changes it: cache and speed are optional. */
export const DEFAULT_ITEMS: readonly StatusItem[] = STATUS_ITEMS.filter(
  (item) => item !== "cache" && item !== "speed",
);
export const ITEMS_KEY = "indus.desktop.statusbar.items";
export const SPEED_WINDOW = 10;

const LABELS: Readonly<Record<StatusItem, MessageKey>> = {
  yolo: "status.item.yolo",
  context: "status.item.context",
  cache: "status.item.cache",
  speed: "status.item.speed",
  workspace: "status.item.workspace",
  model: "status.item.model",
  approvals: "status.item.approvals",
  timer: "status.item.timer",
  version: "status.item.version",
  daemon: "status.item.daemon",
};

// -- session facts from the event stream ---------------------------------------------------------

export interface SpeedSample {
  readonly tokens: number;
  readonly ms: number;
}

export interface SessionFacts {
  readonly info: SessionInfo | null;
  readonly usage: UsageNumbers | null;
  readonly turnStartedAt: number | null;
  /** Output tokens and time of the most recent model calls (newest last, at most 10). */
  readonly samples: readonly SpeedSample[];
  /** When the last call (or the turn) began, for the next sample. */
  readonly lastAt: number | null;
}

export const NO_FACTS: SessionFacts = {
  info: null,
  usage: null,
  turnStartedAt: null,
  samples: [],
  lastAt: null,
};

/** Fold one event into the facts; `now` is when it arrived. */
export function foldFacts(facts: SessionFacts, event: SessionEvent, now: number): SessionFacts {
  switch (event.type) {
    case "session.info":
      return { ...facts, info: event };
    case "turn.started":
      return { ...facts, turnStartedAt: now, lastAt: now };
    case "turn.done":
      return { ...facts, turnStartedAt: null, lastAt: null };
    case "session.usage": {
      const before = facts.usage;
      const usage = event.usage;
      const calls = (usage.api_calls ?? 0) - (before?.api_calls ?? 0);
      let samples = facts.samples;
      const since = facts.lastAt;
      if (before !== null && calls > 0 && since !== null && now > since) {
        const tokens = (usage.output_tokens ?? 0) - (before.output_tokens ?? 0);
        if (tokens >= 0) {
          samples = [...samples, { tokens, ms: now - since }].slice(-SPEED_WINDOW);
        }
      }
      return { ...facts, usage, samples, lastAt: now };
    }
    default:
      return facts;
  }
}

/** Mean tokens per second over the samples (null with none). */
export function tokensPerSecond(samples: readonly SpeedSample[]): number | null {
  const window = samples.slice(-SPEED_WINDOW);
  const ms = window.reduce((sum, s) => sum + s.ms, 0);
  if (window.length === 0 || ms <= 0) return null;
  return window.reduce((sum, s) => sum + s.tokens, 0) / (ms / 1000);
}

/** Share of prompt tokens served from the cache (null when nothing was read). */
export function cacheHitRate(usage: UsageNumbers | null): number | null {
  if (usage === null) return null;
  const cached = usage.cache_read_tokens ?? 0;
  const total = cached + (usage.input_tokens ?? 0) + (usage.cache_write_tokens ?? 0);
  return total > 0 ? cached / total : null;
}

export function contextShare(usage: UsageNumbers | null): number | null {
  const used = usage?.context_tokens;
  const window = usage?.context_window;
  if (used == null || window == null || window <= 0) return null;
  return Math.min(1, used / window);
}

export function useSessionFacts(
  connection: ChatConnection | null,
  sessionId: string | null,
  now: () => number = Date.now,
): SessionFacts {
  const [facts, setFacts] = useState<SessionFacts>(NO_FACTS);
  const nowRef = useRef(now);
  nowRef.current = now;
  useEffect(() => {
    setFacts(NO_FACTS);
    if (connection === null || sessionId === null) return;
    return connection.onSessionEvent(sessionId, (event) => {
      const at = nowRef.current();
      setFacts((current) => foldFacts(current, event, at));
    });
  }, [connection, sessionId]);
  return facts;
}

// -- item visibility -----------------------------------------------------------------------------

function defaultStorage(): StorageLike | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null;
  }
}

export function loadItems(storage: StorageLike | null): StatusItem[] {
  try {
    const raw = storage?.getItem(ITEMS_KEY);
    if (raw == null) return [...DEFAULT_ITEMS];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [...DEFAULT_ITEMS];
    return STATUS_ITEMS.filter((item) => parsed.includes(item));
  } catch {
    return [...DEFAULT_ITEMS];
  }
}

function saveItems(storage: StorageLike | null, items: readonly StatusItem[]): void {
  try {
    storage?.setItem(ITEMS_KEY, JSON.stringify(items));
  } catch {
    // best effort
  }
}

// -- the bar -------------------------------------------------------------------------------------

export interface StatusBarProps {
  sessionId: string | null;
  connection?: ChatConnection | null;
  /** Open server requests; defaults to the store's list for the session. */
  requests?: readonly OpenServerRequest[];
  storage?: StorageLike | null;
  now?: () => number;
  /** REST access for the provider check; defaults to the bridge (tests pass a fake). */
  api?: RestLike | null;
}

/** "Backend needs setup" while no model provider is configured (W52-g); opens onboarding. */
function ProviderSetup(props: { api?: RestLike | null | undefined }) {
  const t = useT();
  const ready = useProviderReady(props.api);
  if (ready !== false) return null;
  return (
    <button
      type="button"
      className="status-setup"
      aria-label={`${t("status.backend")} ${t("status.needsSetup")}`}
      onClick={() => requestOnboarding({ reason: "manual" })}
    >
      <CircleAlert aria-hidden="true" size={12} />
      <span className="status-setup-lead">{t("status.backend")}</span>
      <span className="status-setup-rest">{t("status.needsSetup")}</span>
    </button>
  );
}

const ITEM = "flex items-center gap-1 px-2 whitespace-nowrap";

function formatElapsed(ms: number): string {
  const seconds = Math.max(0, Math.floor(ms / 1000));
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${String(seconds % 60).padStart(2, "0")}`;
}

function Timer(props: { startedAt: number; now: () => number }) {
  const [, tick] = useState(0);
  useEffect(() => {
    const timer = setInterval(() => tick((n) => n + 1), 1000);
    return () => clearInterval(timer);
  }, []);
  return <span data-testid="turn-timer">{formatElapsed(props.now() - props.startedAt)}</span>;
}

export function StatusBar(props: StatusBarProps) {
  const t = useT();
  const active = useGatewayConnection();
  const connection = props.connection === undefined ? active : props.connection;
  const now = props.now ?? Date.now;
  const facts = useSessionFacts(connection, props.sessionId, now);
  const fromStore = useOpenServerRequests(props.requests === undefined ? props.sessionId : null);
  const requests = props.requests ?? fromStore;
  const storage = props.storage === undefined ? defaultStorage() : props.storage;
  const [items, setItems] = useState<StatusItem[]>(() => loadItems(storage));
  const [menu, setMenu] = useState<"none" | "context" | "daemon" | "customise">("none");
  const [yolo, setYolo] = useState<boolean | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setYolo(null);
    if (connection === null || props.sessionId === null) return;
    let live = true;
    connection
      .request("config.get", { key: "yolo", session_id: props.sessionId })
      .then((answer) => {
        if (live) setYolo(answer.value === true);
      })
      .catch(() => {});
    return () => {
      live = false;
    };
  }, [connection, props.sessionId]);

  const toggleYolo = useCallback(async () => {
    if (connection === null || props.sessionId === null) return;
    const next = !(yolo ?? false);
    try {
      const answer = await connection.request("config.set", {
        key: "yolo",
        value: next,
        session_id: props.sessionId,
      });
      setYolo(answer.value === true);
      setError(null);
    } catch (failure) {
      setError(errorMessage(failure));
    }
  }, [connection, props.sessionId, yolo]);

  const shown = new Set(items);
  const toggleItem = (item: StatusItem) => {
    const next = shown.has(item)
      ? items.filter((i) => i !== item)
      : STATUS_ITEMS.filter((i) => i === item || shown.has(i));
    setItems(next);
    saveItems(storage, next);
  };

  const usage = facts.usage;
  const share = contextShare(usage);
  const hit = cacheHitRate(usage);
  const speed = tokensPerSecond(facts.samples);
  const approvals = requests.filter((open) => open.kind === "approval").length;
  const hasSession = props.sessionId !== null;

  return (
    <footer
      role="status"
      aria-label={t("status.label")}
      className="relative flex h-7 items-center border-t border-line bg-raised text-xs text-muted"
    >
      <ProviderSetup api={props.api} />
      {shown.has("yolo") && hasSession ? (
        <button
          type="button"
          role="switch"
          aria-checked={yolo === true}
          title={t("status.yoloHint")}
          className={`${ITEM} ${yolo ? "text-warn" : ""}`}
          onClick={() => void toggleYolo()}
        >
          {t("status.yolo")}
        </button>
      ) : null}

      {shown.has("context") && share !== null ? (
        <button
          type="button"
          aria-haspopup="dialog"
          aria-expanded={menu === "context"}
          className={ITEM}
          onClick={() => setMenu(menu === "context" ? "none" : "context")}
        >
          <meter aria-label={t("status.context")} min={0} max={1} value={share} className="w-12" />
          <span>{Math.round(share * 100)}%</span>
        </button>
      ) : null}

      {shown.has("cache") && hit !== null ? (
        <span className={ITEM} data-testid="cache-hit">
          {t("status.cache", { percent: Math.round(hit * 100) })}
        </span>
      ) : null}

      {shown.has("speed") && speed !== null ? (
        <span className={ITEM} data-testid="speed">
          {t("status.speed", { rate: speed.toFixed(1) })}
        </span>
      ) : null}

      {shown.has("workspace") && facts.info?.cwd ? (
        <span className={`${ITEM} truncate max-w-64`} title={facts.info.cwd}>
          {facts.info.cwd}
        </span>
      ) : null}

      {shown.has("model") && facts.info ? (
        <span className={ITEM}>
          {facts.info.model}
          {facts.info.reasoning_effort ? ` · ${facts.info.reasoning_effort}` : ""}
        </span>
      ) : null}

      {shown.has("approvals") && approvals > 0 ? (
        <span className={`${ITEM} text-warn`}>{t("status.approvals", { count: approvals })}</span>
      ) : null}

      {shown.has("timer") && facts.turnStartedAt !== null ? (
        <span className={ITEM}>
          <Timer startedAt={facts.turnStartedAt} now={now} />
        </span>
      ) : null}

      <span className="flex-1" />

      {error !== null ? (
        <span role="alert" className={`${ITEM} text-danger`}>
          {error}
        </span>
      ) : null}

      {shown.has("version") && connection !== null && connection.codeVersion !== null ? (
        <span className={ITEM}>{t("status.version", { version: connection.codeVersion })}</span>
      ) : null}

      {shown.has("daemon") ? (
        <button
          type="button"
          aria-haspopup="menu"
          aria-expanded={menu === "daemon"}
          className={ITEM}
          onClick={() => setMenu(menu === "daemon" ? "none" : "daemon")}
        >
          {connection === null ? t("status.daemonOffline") : t("status.daemon")}
        </button>
      ) : null}

      <button
        type="button"
        aria-haspopup="menu"
        aria-expanded={menu === "customise"}
        aria-label={t("status.customise")}
        className={ITEM}
        onClick={() => setMenu(menu === "customise" ? "none" : "customise")}
      >
        ⋯
      </button>

      {menu === "context" && usage !== null ? (
        <div
          role="dialog"
          aria-label={t("status.contextBreakdown")}
          className="absolute bottom-full left-0 mb-1 rounded border border-line bg-raised p-2 shadow"
        >
          <dl className="grid grid-cols-2 gap-x-3">
            <dt>{t("status.contextUsed")}</dt>
            <dd>
              {usage.context_tokens ?? 0} / {usage.context_window ?? 0}
            </dd>
            <dt>{t("status.input")}</dt>
            <dd>{usage.input_tokens ?? 0}</dd>
            <dt>{t("status.output")}</dt>
            <dd>{usage.output_tokens ?? 0}</dd>
            <dt>{t("status.cacheRead")}</dt>
            <dd>{usage.cache_read_tokens ?? 0}</dd>
            <dt>{t("status.cacheWrite")}</dt>
            <dd>{usage.cache_write_tokens ?? 0}</dd>
            <dt>{t("status.reasoning")}</dt>
            <dd>{usage.reasoning_tokens ?? 0}</dd>
          </dl>
        </div>
      ) : null}

      {menu === "daemon" ? (
        <div
          role="menu"
          aria-label={t("status.daemon")}
          className="absolute bottom-full right-8 mb-1 rounded border border-line bg-raised p-1 shadow"
        >
          <button
            type="button"
            role="menuitem"
            disabled={connection === null}
            className="block w-full px-2 py-1 text-left"
            onClick={() => {
              connection?.reconnect();
              setMenu("none");
            }}
          >
            {t("status.reconnect")}
          </button>
        </div>
      ) : null}

      {menu === "customise" ? (
        <div
          role="menu"
          aria-label={t("status.customise")}
          className="absolute bottom-full right-0 mb-1 rounded border border-line bg-raised p-1 shadow"
        >
          {STATUS_ITEMS.map((item) => (
            <button
              key={item}
              type="button"
              role="menuitemcheckbox"
              aria-checked={shown.has(item)}
              className="block w-full px-2 py-0.5 text-left"
              onClick={() => toggleItem(item)}
            >
              {shown.has(item) ? "✓ " : "  "}
              {t(LABELS[item])}
            </button>
          ))}
        </div>
      ) : null}
      <ContributionArea area="status-bar" />
    </footer>
  );
}
