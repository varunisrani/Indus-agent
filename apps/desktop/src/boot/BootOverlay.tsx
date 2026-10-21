// The boot overlay (P20-16): main's boot snapshot (`boot.get` + `boot.onProgress`) and the
// installer's events become one of: connecting, install, first run, failure, reauth. A
// retryable, unlatched failure is retried here (bootRetry.ts) before the failure view shows.
// The overlay is modal: Escape never dismisses it, focus stays inside.

import { type Clock, systemClock } from "@indus/client";
import { AlertTriangle, KeyRound } from "lucide-react";
import {
  type KeyboardEvent,
  type ReactNode,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
} from "react";
import { useLocation, useNavigate } from "react-router";
import { desktopBridge } from "../app/bridge.ts";
import { Loader } from "../app/Loader.tsx";
import { classifyPath, overlayPath, overlayStateFrom } from "../app/routes.ts";
import type { IndusBackendNotice, IndusBootProgress, IndusDesktopBoot } from "../global.d.ts";
import { type MessageKey, useT } from "../i18n/index.tsx";
import { BootRetry } from "./bootRetry.ts";
import {
  bootView,
  EMPTY_INSTALLER,
  INSTALL_LOG_SHOWN,
  type InstallerState,
  reduceInstaller,
  type StageState,
} from "./bootState.ts";
import { type FirstRunChoice, FirstRunChooser } from "./FirstRunChooser.tsx";

export type BootBridge = Pick<
  IndusDesktopBoot,
  "get" | "onProgress" | "retry" | "choose" | "onInstaller" | "cancelInstall"
>;

/** `indus:backend:notice` (P20-13): the supervised backend restarted, or keeps crashing. */
export type BackendNotice = IndusBackendNotice;

export type NoticeSource = (listener: (notice: BackendNotice) => void) => () => void;

/** The bridge's `boot.onNotice` event. */
function defaultNotices(): NoticeSource | null {
  return desktopBridge()?.boot.onNotice ?? null;
}

export interface BootOverlayProps {
  /** Default: `window.indusDesktop.boot`; null means no main process (treated as ready). */
  bridge?: BootBridge | null;
  onReady?: () => void;
  /** Default: the bridge's backend notice event; null for none. */
  notices?: NoticeSource | null;
  clock?: Clock;
  random?: () => number;
}

const PHASE_KEYS: Readonly<Record<IndusBootProgress["phase"], MessageKey>> = {
  resolve: "boot.phase.resolve",
  runtime: "boot.phase.runtime",
  "remote-connect": "boot.phase.remote-connect",
  "update-wait": "boot.phase.update-wait",
  "first-run": "boot.phase.first-run",
  ready: "boot.phase.ready",
  error: "boot.phase.error",
};

const STAGE_KEYS: Readonly<Record<StageState, MessageKey>> = {
  pending: "boot.stage.pending",
  running: "boot.stage.running",
  succeeded: "boot.stage.succeeded",
  skipped: "boot.stage.skipped",
  failed: "boot.stage.failed",
};

const READY: IndusBootProgress = {
  phase: "ready",
  message: "",
  progress: 1,
  running: false,
};

function useBootProgress(bridge: BootBridge | null): IndusBootProgress | null {
  const [progress, setProgress] = useState<IndusBootProgress | null>(
    bridge === null ? READY : null,
  );
  useEffect(() => {
    if (bridge === null) return;
    let live = true;
    let pushed = false;
    const off = bridge.onProgress((next) => {
      pushed = true;
      if (live) setProgress(next);
    });
    bridge.get().then(
      (snapshot) => {
        // An event that arrived meanwhile is newer than this answer.
        if (live && !pushed) setProgress(snapshot);
      },
      (error: unknown) => {
        const text = error instanceof Error ? error.message : String(error);
        // No boot handler in main yet (dev without the orchestrator): nothing to wait for.
        if (live && !pushed && text.startsWith("not-available")) setProgress(READY);
      },
    );
    return () => {
      live = false;
      off();
    };
  }, [bridge]);
  return progress;
}

function useInstaller(bridge: BootBridge | null): InstallerState {
  const [state, dispatch] = useReducer(reduceInstaller, EMPTY_INSTALLER);
  useEffect(() => {
    if (bridge === null) return;
    return bridge.onInstaller(dispatch);
  }, [bridge]);
  return state;
}

function InstallView(props: { installer: InstallerState; onCancel: () => void }) {
  const t = useT();
  const tail = props.installer.log.slice(-INSTALL_LOG_SHOWN);
  return (
    <div className="boot-install">
      <h1 id="boot-title">{t("boot.install.title")}</h1>
      <h2 className="boot-subtitle">{t("boot.install.stages")}</h2>
      <ol className="boot-stages">
        {props.installer.stages.map((stage) => (
          <li key={stage.name} data-state={stage.state}>
            {stage.state === "running" ? (
              <Loader size="sm" />
            ) : (
              <span className="stage-dot" aria-hidden="true" />
            )}
            <span className="stage-title">{stage.title}</span>
            <span className="stage-state">{t(STAGE_KEYS[stage.state])}</span>
            {stage.error !== null && <span className="stage-error">{stage.error}</span>}
          </li>
        ))}
      </ol>
      {tail.length > 0 && (
        <section aria-label={t("boot.install.log")}>
          <pre className="log-view">{tail.join("\n")}</pre>
        </section>
      )}
      <button type="button" className="text-button" onClick={props.onCancel}>
        {t("boot.install.cancel")}
      </button>
    </div>
  );
}

function CrashNotice(props: { notice: Extract<BackendNotice, { kind: "crashing" }> }) {
  const t = useT();
  const { notice } = props;
  const tail = notice.recentOutput.filter((line) => line.trim() !== "").slice(-INSTALL_LOG_SHOWN);
  return (
    <section aria-label={t("boot.crash.title")}>
      <p>
        {notice.exitCode === null
          ? notice.message
          : t("boot.crash.exit", { message: notice.message, code: notice.exitCode })}
      </p>
      {tail.length > 0 && <pre className="log-view">{tail.join("\n")}</pre>}
    </section>
  );
}

export function BootOverlay(props: BootOverlayProps) {
  const t = useT();
  const navigate = useNavigate();
  const location = useLocation();
  const bridge = props.bridge === undefined ? (desktopBridge()?.boot ?? null) : props.bridge;
  const progress = useBootProgress(bridge);
  const installer = useInstaller(bridge);
  const notices = props.notices === undefined ? defaultNotices() : props.notices;
  const [notice, setNotice] = useState<BackendNotice | null>(null);
  useEffect(() => (notices === null ? undefined : notices(setNotice)), [notices]);
  const view = bootView(progress, installer);
  const clock = props.clock ?? systemClock;
  const random = props.random;
  const retry = useMemo(
    () => new BootRetry({ clock, ...(random === undefined ? {} : { random }) }),
    [clock, random],
  );
  const [, tick] = useReducer((n: number) => n + 1, 0);
  const [busy, setBusy] = useState(false);
  const readyFired = useRef(false);
  const root = useRef<HTMLDivElement>(null);
  const onReady = props.onReady;

  useEffect(() => {
    if (view.kind !== "ready") {
      readyFired.current = false;
      return;
    }
    retry.reset();
    if (!readyFired.current) {
      readyFired.current = true;
      onReady?.();
    }
  }, [view.kind, retry, onReady]);

  const autoRetrying =
    view.kind === "failure" &&
    view.retryable &&
    !view.latched &&
    (!retry.exhausted || retry.pending);

  // Each new failure snapshot may schedule the next attempt (one waits at a time).
  // biome-ignore lint/correctness/useExhaustiveDependencies: `progress` marks a new snapshot
  useEffect(() => {
    if (!autoRetrying || bridge === null || retry.pending) return;
    retry.schedule(() => {
      tick();
      void bridge.retry({ action: "retry" }).catch(() => undefined);
    });
    tick();
  }, [autoRetrying, bridge, retry, progress]);

  useEffect(() => () => retry.cancel(), [retry]);

  useEffect(() => {
    if (view.kind !== "ready") root.current?.focus();
  }, [view.kind]);

  if (view.kind === "ready") return null;
  // The Settings button opens the settings overlay above everything; give it the window.
  const route = classifyPath(location.pathname);
  if (route.kind === "overlay" && route.overlay === "settings") return null;

  const act = (run: () => Promise<unknown>) => {
    setBusy(true);
    run()
      .catch(() => undefined)
      .finally(() => setBusy(false));
  };
  const manualRetry = (action: "retry" | "repair") => {
    if (bridge === null) return;
    retry.reset();
    act(() => bridge.retry({ action }));
  };
  const openSettings = () =>
    navigate(overlayPath("settings"), {
      state: overlayStateFrom(location.pathname, location.state),
    });
  const choose = (choice: FirstRunChoice) => {
    if (bridge !== null) act(() => bridge.choose({ choice }));
  };
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
    }
  };

  let body: ReactNode;
  if (view.kind === "install") {
    body = (
      <InstallView
        installer={view.installer}
        onCancel={() => {
          if (bridge !== null) act(() => bridge.cancelInstall());
        }}
      />
    );
  } else if (view.kind === "first-run") {
    body = <FirstRunChooser onChoose={choose} busy={busy} />;
  } else if (view.kind === "reauth") {
    body = (
      <div className="boot-failure">
        <KeyRound aria-hidden="true" size={20} />
        <h1 id="boot-title">{t("boot.reauth.title")}</h1>
        <p>{t("boot.reauth.body")}</p>
        {view.message !== "" && <pre className="log-view">{view.message}</pre>}
        <button type="button" className="button button-primary" onClick={openSettings}>
          {t("boot.reauth.action")}
        </button>
      </div>
    );
  } else if (view.kind === "failure" && !autoRetrying) {
    body = (
      <div className="boot-failure">
        <AlertTriangle aria-hidden="true" size={20} />
        <h1 id="boot-title">{t("boot.failure.title")}</h1>
        {view.message !== "" && <pre className="log-view">{view.message}</pre>}
        {view.latched && <p>{t("boot.failure.latched")}</p>}
        {notice?.kind === "crashing" && <CrashNotice notice={notice} />}
        <div className="boot-actions">
          {!view.latched && (
            <button
              type="button"
              className="button button-primary"
              disabled={busy}
              onClick={() => manualRetry("retry")}
            >
              {t("boot.failure.retry")}
            </button>
          )}
          <button
            type="button"
            className={view.latched ? "button button-primary" : "button button-secondary"}
            disabled={busy}
            onClick={() => manualRetry("repair")}
          >
            {t("boot.failure.repair")}
          </button>
          <button type="button" className="button button-secondary" onClick={openSettings}>
            {t("boot.failure.settings")}
          </button>
        </div>
      </div>
    );
  } else {
    const phase = view.kind === "connecting" ? view.phase : "runtime";
    const due = retry.dueAt;
    body = (
      <div className="boot-connecting">
        <Loader label={t(PHASE_KEYS[phase])} />
        <h1 id="boot-title">{t(PHASE_KEYS[phase])}</h1>
        {view.kind === "connecting" && view.message !== "" && <p>{view.message}</p>}
        {notice?.kind === "restarted" && <p className="boot-note">{notice.message}</p>}
        {view.kind === "failure" && due !== null && (
          <p>
            {t("boot.failure.retrying", {
              seconds: Math.max(1, Math.ceil((due - clock.now()) / 1000)),
              attempt: retry.attempts,
              max: retry.max,
            })}
          </p>
        )}
      </div>
    );
  }

  return (
    <div
      ref={root}
      className="boot-overlay"
      role="dialog"
      aria-modal="true"
      aria-labelledby="boot-title"
      data-view={view.kind}
      tabIndex={-1}
      onKeyDown={onKeyDown}
    >
      <div className="boot-card">{body}</div>
    </div>
  );
}
