// The ambient side of an app window (P20-35..37), mounted once inside the router of main,
// secondary and instance windows. It renders only toasts; the rest is wiring:
//
// - reports the session the route shows (and re-reports on focus) so main knows the last-active
//   conversation for quick entry and the HUD toggle;
// - reports running turns (`work.report`: the quit guard and keep awake);
// - notifies a finished turn or a waiting question while the window is not focused, with our own
//   chime when sounds are on; a click opens that session;
// - receives quick-entry text and submits it through the composer's send path, then answers main;
// - shows the one-time keep-awake offer, update availability and skew notices.

import "./ambient.css";
import { useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router";
import { desktopBridge, launchFacts } from "../app/bridge.ts";
import { backgroundOf, classifyPath, sessionPath } from "../app/routes.ts";
import { ChatActions, type Request, sharedActions } from "../chat/busyActions.ts";
import { InflightJournal } from "../chat/inflightJournal.ts";
import type { TranscriptStore } from "../chat/transcriptStore.ts";
import type { GatewayConnection } from "../gateway/connectionStore.ts";
import type {
  IndusAmbientState,
  IndusPrefsSnapshot,
  IndusUpdateSkew,
  IndusUpdateStatus,
} from "../global.d.ts";
import { useT } from "../i18n/index.tsx";
import { deliverQuickText } from "../quick/deliver.ts";
import { runtimeAhead, watchChunkFailures } from "../updates/skew.ts";
import { UpdateNotice } from "../updates/UpdateNotice.tsx";
import { playChime } from "./sound.ts";
import {
  type NoticeRequest,
  requestNotice,
  tagSession,
  turnNotice,
  WorkTracker,
} from "./turnNotices.ts";

function newKey(): string {
  return (
    globalThis.crypto?.randomUUID?.() ?? `q-${Date.now()}-${Math.random().toString(36).slice(2)}`
  );
}

function focusedNow(): boolean {
  return (
    typeof document !== "undefined" && document.visibilityState === "visible" && document.hasFocus()
  );
}

export interface AmbientHostProps {
  connection: GatewayConnection;
  transcripts: TranscriptStore;
}

export function AmbientHost(props: AmbientHostProps) {
  const { connection, transcripts } = props;
  const t = useT();
  const location = useLocation();
  const navigate = useNavigate();
  const bridge = desktopBridge();
  const inflight = useMemo(() => new InflightJournal(), []);
  const ambient = useRef<IndusAmbientState | null>(null);
  // which kinds of notice the person wants (Settings › Notifications, P20-32)
  const noticeKinds = useRef<IndusPrefsSnapshot["notifications"] | null>(null);
  const [offer, setOffer] = useState(false);
  const [update, setUpdate] = useState<IndusUpdateStatus | null>(null);
  const [skew, setSkew] = useState<IndusUpdateSkew | null>(null);

  // -- the route's session → main --------------------------------------------------------------
  const view = classifyPath(location.pathname);
  const shown = view.kind === "overlay" ? classifyPath(backgroundOf(location.state) ?? "/") : view;
  const sessionId = shown.kind === "chat" ? shown.sessionId : null;
  useEffect(() => {
    if (bridge === null) return;
    bridge.windows.reportSession({ sessionId });
    const onFocus = () => bridge.windows.reportSession({ sessionId });
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [bridge, sessionId]);

  // -- settings ----------------------------------------------------------------------------------
  useEffect(() => {
    if (bridge === null) return;
    let live = true;
    bridge.ambient.get().then(
      (state) => {
        if (live) ambient.current = state;
      },
      () => {},
    );
    const offChanged = bridge.ambient.onChanged((state) => {
      ambient.current = state;
    });
    const offOffer = bridge.ambient.onKeepAwakeOffer(() => setOffer(true));
    bridge.prefs.get().then(
      (prefs) => {
        if (live) noticeKinds.current = prefs.notifications;
      },
      () => {},
    );
    const offPrefs = bridge.prefs.onChanged((prefs) => {
      noticeKinds.current = prefs.notifications;
    });
    return () => {
      live = false;
      offChanged();
      offOffer();
      offPrefs();
    };
  }, [bridge]);

  // -- running work and notifications -----------------------------------------------------------
  useEffect(() => {
    if (bridge === null) return;
    const work = new WorkTracker();
    const notify = (request: NoticeRequest | null) => {
      if (request === null) return;
      bridge.notifications.show(request).then(
        ({ shown: wasShown }) => {
          if (wasShown && ambient.current?.sounds !== false) playChime();
        },
        () => {},
      );
    };
    const offEvents = connection.onEvent((event) => {
      const record = event as { type: string; session_id?: string; turn_id?: string };
      if (work.onEvent(record)) bridge.work.report({ items: work.items() });
      if (record.type === "turn.done" && noticeKinds.current?.turnComplete !== false) {
        notify(turnNotice(event as Parameters<typeof turnNotice>[0], focusedNow(), t));
      }
    });
    const seen = new Set<string>();
    const store = connection.serverRequests;
    const offRequests = store.subscribe(() => {
      for (const request of store.list()) {
        if (seen.has(request.id)) continue;
        seen.add(request.id);
        if (!request.replayed && noticeKinds.current?.approvals !== false)
          notify(requestNotice(request.kind, request.sessionId, focusedNow(), t));
      }
    });
    const offAction = bridge.notifications.onAction(({ tag }) => {
      const target = tagSession(tag);
      if (target !== null) navigate(sessionPath(target));
    });
    return () => {
      offEvents();
      offRequests();
      offAction();
      if (work.clear()) bridge.work.report({ items: [] });
    };
  }, [bridge, connection, navigate, t]);

  // -- quick entry delivery ----------------------------------------------------------------------
  useEffect(() => {
    if (bridge === null) return;
    return bridge.quick.onDeliver((delivery) => {
      void deliverQuickText(
        {
          connection,
          rows: transcripts,
          actionsFor: (id) =>
            sharedActions(
              connection,
              id,
              () =>
                new ChatActions({
                  sessionId: id,
                  request: connection.request.bind(connection) as Request,
                  inflight,
                }),
            ),
          newKey,
          onSessionCreated: (id) => navigate(sessionPath(id)),
        },
        delivery,
      )
        .catch((error: unknown) => ({
          ok: false as const,
          sessionId: delivery.sessionId,
          error: error instanceof Error ? error.message : "the message could not be sent",
        }))
        .then((result) =>
          bridge.quick.ack({
            requestId: delivery.requestId,
            ok: result.ok,
            sessionId: result.sessionId,
            ...(result.ok ? {} : { error: result.error.slice(0, 500) }),
          }),
        );
    });
  }, [bridge, connection, transcripts, inflight, navigate]);

  // -- updates and skew --------------------------------------------------------------------------
  useEffect(() => {
    if (bridge === null) return;
    let live = true;
    bridge.updates.status().then(
      (status) => {
        if (live) setUpdate(status);
      },
      () => {},
    );
    const offProgress = bridge.updates.onProgress(setUpdate);
    const offSkew = bridge.updates.onSkew(setSkew);
    const offChunks = watchChunkFailures(window, () =>
      setSkew({ kind: "renderer-stale", detail: "" }),
    );
    return () => {
      live = false;
      offProgress();
      offSkew();
      offChunks();
    };
  }, [bridge]);

  useEffect(() => {
    const facts = launchFacts();
    const check = () => {
      const version = connection.codeVersion;
      if (runtimeAhead(facts.appVersion, version, facts.packaged))
        setSkew((now) => now ?? { kind: "app-behind-runtime", detail: "" });
    };
    check();
    return connection.subscribe(check);
  }, [connection]);

  if (bridge === null) return null;
  return (
    <div className="ambient-toasts" aria-live="polite">
      {offer && (
        <div className="toast ambient-toast" role="status">
          <p>{t("keepAwake.offer")}</p>
          <div className="ambient-actions">
            <button
              type="button"
              className="button button-primary"
              onClick={() => {
                setOffer(false);
                void bridge.ambient.set({ keepAwake: "while-working" }).catch(() => {});
              }}
            >
              {t("keepAwake.accept")}
            </button>
            <button
              type="button"
              className="button button-secondary"
              onClick={() => setOffer(false)}
            >
              {t("keepAwake.dismiss")}
            </button>
          </div>
        </div>
      )}
      <UpdateNotice status={update} skew={skew} onDismissSkew={() => setSkew(null)} />
    </div>
  );
}
