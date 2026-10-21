// The preview pane (P20-28): a web page in a `<webview>` on the persistent preview partition, or a
// local file (text, image, or an HTML file in the webview) with live reload.
//
// Security: the webview never gets `allowpopups`; main forces the guest preload, Node off,
// isolation and sandbox on. The guest preload hands a trusted click on a `_blank` link to this page
// (`ipc-message` on the webview); this pane admits http/https only and asks main to open it with
// `external.open({url, guestContentsId})` — main checks the guest belongs to this window and saw a
// real input (D-16.20, R08.8). Our own "Open in browser" button is a real click in this window.

import {
  ArrowLeft,
  ArrowRight,
  Camera,
  ExternalLink,
  MapPin,
  PictureInPicture2,
  RotateCw,
  Send,
  X,
} from "lucide-react";
import {
  type FormEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import { desktopBridge } from "../../app/bridge.ts";
import { Loader } from "../../app/Loader.tsx";
import type { IndusDesktopBridge } from "../../global.d.ts";
import { useT } from "../../i18n/index.tsx";
import { AnnotateSession, type GuestPick, guestCall, guestInstallScript } from "./annotate.ts";
import { windowPreviewAutomation } from "./automation.ts";
import {
  addressOf,
  fileUrlOf,
  fileViewOf,
  formatBytes,
  GUEST_LINK_CHANNEL,
  isWebUrl,
  PREVIEW_PARTITION,
  type PreviewTarget,
  parseAddress,
} from "./target.ts";

export type PreviewBridge = Pick<IndusDesktopBridge, "fs" | "preview" | "external">;

/** The webview element's methods the pane uses (Electron's `WebviewTag`). */
interface WebviewElement extends HTMLElement {
  getWebContentsId?(): number;
  canGoBack?(): boolean;
  canGoForward?(): boolean;
  goBack?(): void;
  goForward?(): void;
  reload?(): void;
  loadURL?(url: string): Promise<void>;
  executeJavaScript?(code: string): Promise<unknown>;
}

/** Run `code` in the guest page (bound: Electron reads `this` inside the method). */
function guestRunner(view: WebviewElement | null): ((code: string) => Promise<unknown>) | null {
  if (view === null || typeof view.executeJavaScript !== "function") return null;
  return (code) => (view.executeJavaScript as (c: string) => Promise<unknown>).call(view, code);
}

export interface PreviewPaneProps {
  target: PreviewTarget | null;
  mode: "local" | "remote" | null;
  /** Hidden by the pane shell: stays mounted (the page keeps its state) but inert. */
  hidden?: boolean;
  bridge?: PreviewBridge | null;
  /** The address field moved to another target. */
  onNavigate?(target: PreviewTarget): void;
  /** Present when the host can show the preview in its own window. */
  onPopOut?(target: PreviewTarget): void;
  /** Present when a captured image can be added to the conversation. */
  onAttachCapture?(dataUrl: string): void;
  /** The session this preview belongs to (the agent may drive it, P24-25). */
  sessionId?: string | null;
  /** Present when annotate notes can be put into the chat (P24-25). */
  onAnnotationSend?(text: string): void;
}

const BUTTON =
  "inline-flex items-center justify-center rounded p-1 text-muted hover:bg-raised hover:text-fg disabled:opacity-40 focus-visible:outline-2 focus-visible:outline-focus";

type FileState =
  | { phase: "loading" }
  | { phase: "text"; text: string; truncated: boolean; shown: number }
  | { phase: "image"; dataUrl: string }
  | { phase: "binary" }
  | { phase: "error"; message: string };

function guestUrlOf(target: PreviewTarget | null): string {
  if (target === null) return "";
  return target.kind === "url" ? target.url : fileUrlOf(target.path);
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/** The webview for one URL; created imperatively so its DOM events can be listened to. */
function GuestView(props: {
  url: string;
  bridge: PreviewBridge | null;
  onState(state: { back: boolean; forward: boolean; url?: string; loading?: boolean }): void;
  onError(message: string | null): void;
  onNotice(message: string | null): void;
  register(view: WebviewElement | null): void;
}) {
  const t = useT();
  const host = useRef<HTMLDivElement | null>(null);
  const { url, bridge, onState, onError, onNotice, register } = props;

  useEffect(() => {
    const parent = host.current;
    if (parent === null) return;
    const view = document.createElement("webview") as WebviewElement;
    view.setAttribute("partition", PREVIEW_PARTITION);
    view.setAttribute("webpreferences", "contextIsolation=yes,sandbox=yes,nodeIntegration=no");
    view.setAttribute("src", url);
    view.className = "h-full w-full flex-1 bg-white";
    const history = () => {
      try {
        onState({ back: view.canGoBack?.() ?? false, forward: view.canGoForward?.() ?? false });
      } catch {
        // not attached yet
      }
    };
    const onIpc = (event: Event) => {
      const message = event as Event & { channel?: string; args?: unknown[] };
      if (message.channel !== GUEST_LINK_CHANNEL) return;
      const link = String(message.args?.[0] ?? "");
      if (!isWebUrl(link)) {
        onNotice(t("preview.blockedLink"));
        return;
      }
      const guestContentsId = view.getWebContentsId?.();
      if (bridge === null || guestContentsId === undefined) return;
      void bridge.external
        .open({ url: link, guestContentsId })
        .then((answer) => onNotice(answer.ok ? null : t("preview.linkFailed")))
        .catch(() => onNotice(t("preview.linkFailed")));
    };
    const onNavigate = (event: Event) => {
      const nav = event as Event & { url?: string; isMainFrame?: boolean };
      if (nav.isMainFrame === false) return;
      onError(null);
      if (typeof nav.url === "string") onState({ back: false, forward: false, url: nav.url });
      history();
    };
    const onFail = (event: Event) => {
      const fail = event as Event & {
        errorCode?: number;
        errorDescription?: string;
        isMainFrame?: boolean;
        validatedURL?: string;
      };
      // A blocked sub-frame or an aborted load (-3) must not replace a healthy page.
      if (fail.isMainFrame === false || fail.errorCode === -3) return;
      onError(fail.errorDescription || fail.validatedURL || String(fail.errorCode ?? ""));
    };
    const onStart = () => onState({ back: false, forward: false, loading: true });
    const onStop = () => {
      onState({ back: false, forward: false, loading: false });
      history();
    };
    view.addEventListener("ipc-message", onIpc);
    view.addEventListener("did-navigate", onNavigate);
    view.addEventListener("did-navigate-in-page", onNavigate);
    view.addEventListener("did-fail-load", onFail);
    view.addEventListener("did-start-loading", onStart);
    view.addEventListener("did-stop-loading", onStop);
    parent.replaceChildren(view);
    register(view);
    return () => {
      view.removeEventListener("ipc-message", onIpc);
      view.removeEventListener("did-navigate", onNavigate);
      view.removeEventListener("did-navigate-in-page", onNavigate);
      view.removeEventListener("did-fail-load", onFail);
      view.removeEventListener("did-start-loading", onStart);
      view.removeEventListener("did-stop-loading", onStop);
      register(null);
      parent.replaceChildren();
    };
    // The guest is built once per URL target; in-page navigation keeps the same guest.
  }, [url, bridge, onState, onError, onNotice, register, t]);

  return <div ref={host} className="flex min-h-0 flex-1" data-testid="preview-guest-host" />;
}

/** A local text or image file, re-read when main reports a change. */
function FileView(props: { path: string; bridge: PreviewBridge; reloadKey: number }) {
  const t = useT();
  const [state, setState] = useState<FileState>({ phase: "loading" });
  const { path, bridge, reloadKey } = props;

  // biome-ignore lint/correctness/useExhaustiveDependencies: reloadKey re-reads the file on a change
  useEffect(() => {
    let live = true;
    const view = fileViewOf(path);
    const load = async (): Promise<FileState> => {
      if (view === "image") {
        const { dataUrl } = await bridge.fs.readDataUrl({ path, purpose: "preview" });
        return { phase: "image", dataUrl };
      }
      const answer = await bridge.fs.readText({ path });
      if (answer.binary) return { phase: "binary" };
      return {
        phase: "text",
        text: answer.text,
        truncated: answer.truncated,
        shown: new TextEncoder().encode(answer.text).length,
      };
    };
    load()
      .then((next) => {
        if (live) setState(next);
      })
      .catch((error: unknown) => {
        if (live) setState({ phase: "error", message: errorMessage(error) });
      });
    return () => {
      live = false;
    };
  }, [path, bridge, reloadKey]);

  if (state.phase === "loading") return <Loader />;
  if (state.phase === "error") {
    return (
      <p role="alert" className="p-3 text-sm text-danger">
        {t("preview.readFailed", { message: state.message })}
      </p>
    );
  }
  if (state.phase === "binary")
    return <p className="p-3 text-sm text-muted">{t("preview.binary")}</p>;
  if (state.phase === "image") {
    return (
      <div className="flex min-h-0 flex-1 items-center justify-center overflow-auto p-3">
        <img src={state.dataUrl} alt={path} className="max-h-full max-w-full" />
      </div>
    );
  }
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {state.truncated ? (
        <p role="status" className="border-b border-line px-3 py-1 text-xs text-warn">
          {t("preview.truncated", { size: formatBytes(state.shown) })}
        </p>
      ) : null}
      <pre className="m-0 min-h-0 flex-1 overflow-auto whitespace-pre-wrap p-3 font-mono text-xs">
        {state.text}
      </pre>
    </div>
  );
}

export function PreviewPane(props: PreviewPaneProps) {
  const t = useT();
  const bridge = props.bridge === undefined ? desktopBridge() : props.bridge;
  const [target, setTarget] = useState<PreviewTarget | null>(props.target);
  const [address, setAddress] = useState(addressOf(props.target));
  const [history, setHistory] = useState({ back: false, forward: false });
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [captured, setCaptured] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  // The URL the guest was created with; in-page and address-bar navigation keep the same guest.
  const [seed, setSeed] = useState(guestUrlOf(props.target));
  const view = useRef<WebviewElement | null>(null);

  // A target from outside (the files pane, the agent's link) replaces the current one.
  useEffect(() => {
    setTarget(props.target);
    setAddress(addressOf(props.target));
    setSeed(guestUrlOf(props.target));
    setLoadError(null);
  }, [props.target]);

  const filePath = target?.kind === "file" ? target.path : null;
  const local = props.mode !== "remote";

  // Live reload: main watches the file (debounced 120 ms) and tells this window.
  useEffect(() => {
    if (filePath === null || !local || bridge === null) return;
    let watchId: string | null = null;
    let live = true;
    const off = bridge.preview.onFileChanged((change) => {
      if (change.watchId !== watchId) return;
      if (fileViewOf(filePath) === "html") view.current?.reload?.();
      else setReloadKey((key) => key + 1);
    });
    bridge.preview
      .watchFile({ path: filePath })
      .then((answer) => {
        if (live) watchId = answer.watchId;
        else void bridge.preview.unwatchFile({ watchId: answer.watchId }).catch(() => {});
      })
      .catch(() => {
        // not watchable (outside the roots, a secret): the file is still shown once
      });
    return () => {
      live = false;
      off();
      if (watchId !== null) void bridge.preview.unwatchFile({ watchId }).catch(() => {});
    };
  }, [filePath, local, bridge]);

  const onGuestState = useCallback(
    (state: { back: boolean; forward: boolean; url?: string; loading?: boolean }) => {
      if (state.loading !== undefined) {
        setLoading(state.loading);
        return;
      }
      if (state.url !== undefined) {
        const url = state.url;
        setAddress(url);
        return;
      }
      setHistory({ back: state.back, forward: state.forward });
    },
    [],
  );
  const sessionRef = useRef<string | null>(props.sessionId ?? null);
  sessionRef.current = props.sessionId ?? null;
  const detachAutomation = useRef<(() => void) | null>(null);
  const register = useCallback((element: WebviewElement | null) => {
    view.current = element;
    detachAutomation.current?.();
    detachAutomation.current = null;
    const run = guestRunner(element);
    if (element === null || run === null) return;
    detachAutomation.current = windowPreviewAutomation.attach({
      executeJavaScript: run,
      loadURL: async (url) => {
        await element.loadURL?.(url);
      },
      sessionId: () => sessionRef.current,
    });
  }, []);
  useEffect(() => () => detachAutomation.current?.(), []);

  // -- annotate mode (P24-25) ---------------------------------------------------------------------
  const onAnnotationSend = props.onAnnotationSend;
  const annotations = useMemo(
    () => new AnnotateSession({ onSend: (text) => onAnnotationSend?.(text) }),
    [onAnnotationSend],
  );
  const pins = useSyncExternalStore(
    (listener) => annotations.subscribe(listener),
    () => annotations.list(),
    () => annotations.list(),
  );
  const [annotating, setAnnotating] = useState(false);
  useEffect(() => {
    if (!annotating) return;
    const run = guestRunner(view.current);
    if (run === null) {
      setAnnotating(false);
      return;
    }
    let live = true;
    const loop = async () => {
      await run(guestInstallScript());
      await run(guestCall("start"));
      while (live) {
        const event = (await run(guestCall("next"))) as { type?: string; pick?: GuestPick } | null;
        if (!live || event === null || event.type !== "pick" || event.pick === undefined) break;
        annotations.add(event.pick);
        await run(
          guestCall(
            "showPins",
            annotations
              .list()
              .map((p) => ({ number: p.number, selector: p.selector, note: p.note })),
          ),
        );
      }
    };
    loop().catch(() => {
      if (live) setAnnotating(false);
    });
    return () => {
      live = false;
      void run(guestCall("stop")).catch(() => {});
    };
  }, [annotating, annotations]);
  const leaveAnnotate = () => {
    annotations.discard();
    setAnnotating(false);
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const next = parseAddress(address);
    if (next === null) {
      setNotice(t("preview.badAddress"));
      return;
    }
    setNotice(null);
    setLoadError(null);
    if (next.kind === "url" && target?.kind === "url" && view.current?.loadURL !== undefined) {
      // The same guest navigates: its history and storage stay.
      void view.current.loadURL(next.url).catch(() => {});
    } else {
      setSeed(guestUrlOf(next));
    }
    setTarget(next);
    props.onNavigate?.(next);
  };

  const capture = async () => {
    const guestContentsId = view.current?.getWebContentsId?.();
    if (bridge === null || guestContentsId === undefined) return;
    try {
      setCaptured((await bridge.preview.capture({ guestContentsId })).dataUrl);
    } catch (error) {
      setNotice(errorMessage(error));
    }
  };

  const openExternal = async () => {
    if (bridge === null || target?.kind !== "url") return;
    // The page the guest shows now (the address follows its navigations).
    const url = isWebUrl(address) ? address : target.url;
    const answer = await bridge.external.open({ url }).catch(() => ({ ok: false as const }));
    if (!answer.ok) setNotice(t("preview.linkFailed"));
  };

  const isWeb = target?.kind === "url";
  const showsGuest =
    target !== null &&
    bridge !== null &&
    (target.kind === "url" || (local && fileViewOf(target.path) === "html"));

  return (
    <section
      className={`flex h-full min-h-0 flex-col ${props.hidden === true ? "invisible" : ""}`}
      aria-label={t("pane.preview")}
      aria-hidden={props.hidden === true ? true : undefined}
      inert={props.hidden === true}
      data-testid="preview-pane"
    >
      <form className="flex items-center gap-1 border-b border-line px-2 py-1" onSubmit={submit}>
        <button
          type="button"
          className={BUTTON}
          aria-label={t("preview.back")}
          disabled={!history.back}
          onClick={() => view.current?.goBack?.()}
        >
          <ArrowLeft size={14} aria-hidden="true" />
        </button>
        <button
          type="button"
          className={BUTTON}
          aria-label={t("preview.forward")}
          disabled={!history.forward}
          onClick={() => view.current?.goForward?.()}
        >
          <ArrowRight size={14} aria-hidden="true" />
        </button>
        <button
          type="button"
          className={BUTTON}
          aria-label={t("preview.reload")}
          disabled={target === null}
          onClick={() => {
            if (showsGuest) view.current?.reload?.();
            else setReloadKey((key) => key + 1);
          }}
        >
          <RotateCw size={14} aria-hidden="true" />
        </button>
        <input
          className="min-w-0 flex-1 rounded border border-line bg-bg px-2 py-0.5 text-sm"
          aria-label={t("preview.address")}
          value={address}
          spellCheck={false}
          onChange={(event) => setAddress(event.target.value)}
        />
        <button type="submit" className="sr-only">
          {t("preview.go")}
        </button>
        {loading ? <Loader /> : null}
        <button
          type="button"
          className={`${BUTTON} ${annotating ? "bg-raised text-accent" : ""}`}
          aria-label={t("annotate.toggle")}
          aria-pressed={annotating}
          disabled={!isWeb || !showsGuest}
          onClick={() => (annotating ? leaveAnnotate() : setAnnotating(true))}
        >
          <MapPin size={14} aria-hidden="true" />
        </button>
        <button
          type="button"
          className={BUTTON}
          aria-label={t("preview.capture")}
          disabled={!showsGuest}
          onClick={() => void capture()}
        >
          <Camera size={14} aria-hidden="true" />
        </button>
        <button
          type="button"
          className={BUTTON}
          aria-label={t("preview.openExternal")}
          disabled={!isWeb}
          onClick={() => void openExternal()}
        >
          <ExternalLink size={14} aria-hidden="true" />
        </button>
        {props.onPopOut !== undefined && target !== null ? (
          <button
            type="button"
            className={BUTTON}
            aria-label={t("preview.popOut")}
            onClick={() => props.onPopOut?.(target)}
          >
            <PictureInPicture2 size={14} aria-hidden="true" />
          </button>
        ) : null}
      </form>

      {notice !== null ? (
        <p role="status" className="border-b border-line px-3 py-1 text-xs text-warn">
          {notice}
        </p>
      ) : null}
      {loadError !== null ? (
        <p role="alert" className="border-b border-line px-3 py-1 text-xs text-danger">
          {t("preview.loadFailed", { message: loadError })}
        </p>
      ) : null}
      {captured !== null ? (
        <figure className="m-0 flex items-center gap-2 border-b border-line px-3 py-1">
          <img
            src={captured}
            alt={t("preview.captured")}
            className="h-12 rounded border border-line"
          />
          {props.onAttachCapture !== undefined ? (
            <button
              type="button"
              className="rounded border border-line px-2 py-0.5 text-xs hover:bg-raised"
              onClick={() => {
                props.onAttachCapture?.(captured);
                setCaptured(null);
              }}
            >
              {t("preview.attachCapture")}
            </button>
          ) : null}
          <button
            type="button"
            className="rounded border border-line px-2 py-0.5 text-xs hover:bg-raised"
            onClick={() => setCaptured(null)}
          >
            {t("preview.dismissCapture")}
          </button>
        </figure>
      ) : null}

      {annotating ? (
        <section
          className="flex max-h-64 flex-col gap-1 overflow-auto border-b border-line px-3 py-2 text-xs"
          aria-label={t("annotate.toggle")}
          data-testid="annotate-panel"
        >
          <p className="text-muted">{t("annotate.hint")}</p>
          {pins.map((pin) => (
            <div key={pin.number} className="flex items-start gap-2">
              <span className="font-semibold">{t("annotate.pin", { number: pin.number })}</span>
              <textarea
                className="min-h-8 flex-1 rounded border border-line bg-bg px-1"
                aria-label={t("annotate.pin", { number: pin.number })}
                placeholder={t("annotate.notePlaceholder")}
                value={pin.note}
                onChange={(event) => annotations.setNote(pin.number, event.target.value)}
              />
              <button
                type="button"
                className={BUTTON}
                aria-label={t("annotate.remove", { number: pin.number })}
                onClick={() => annotations.remove(pin.number)}
              >
                <X size={12} aria-hidden="true" />
              </button>
            </div>
          ))}
          <div className="flex gap-2">
            <button
              type="button"
              className="inline-flex items-center gap-1 rounded border border-line px-2 py-0.5 hover:bg-raised disabled:opacity-40"
              disabled={pins.length === 0 || onAnnotationSend === undefined}
              onClick={() => {
                annotations.send(address);
                setAnnotating(false);
              }}
            >
              <Send size={12} aria-hidden="true" />
              {t("annotate.send")}
            </button>
            <button
              type="button"
              className="rounded border border-line px-2 py-0.5 hover:bg-raised"
              onClick={leaveAnnotate}
            >
              {t("annotate.done")}
            </button>
          </div>
        </section>
      ) : null}

      <div className="flex min-h-0 flex-1 flex-col">
        {target === null ? (
          <p className="p-3 text-sm text-muted">{t("preview.empty")}</p>
        ) : showsGuest ? (
          <GuestView
            url={seed}
            bridge={bridge}
            onState={onGuestState}
            onError={setLoadError}
            onNotice={setNotice}
            register={register}
          />
        ) : target.kind === "file" && !local ? (
          <p className="p-3 text-sm text-muted">{t("preview.remoteFile")}</p>
        ) : target.kind === "file" && bridge !== null ? (
          <FileView path={target.path} bridge={bridge} reloadKey={reloadKey} />
        ) : (
          <p className="p-3 text-sm text-muted">{t("pane.unavailable")}</p>
        )}
      </div>
    </section>
  );
}
