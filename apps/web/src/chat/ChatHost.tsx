// The Chat tab (P18-26): the TUI in xterm over `/api/pty`, with the right rail (P18-27) beside it.
//
// The shell mounts this once, on the first visit to `/chat`, and afterwards only hides it
// (`active` false), so the terminal and its socket survive other tabs (R09.6). This component
// wires three things together:
// - the terminal view (`terminal.ts`, xterm; a fake in tests),
// - the PTY connection (`ptySocket.ts`): channel id per mount, this tab's attach token
//   (`attachToken.ts`), `?resume=` from the URL, the profile switcher, the picked workspace,
// - the rail, inside an error boundary so nothing it does can take the terminal down (R09.1).
//
// "Start new session" and the rail's "New chat" rotate the attach token and dial with `fresh=1`;
// a session picked in the rail (or a `/chat?resume=<id>` link from the Sessions page) redials in
// place with that resume id.

import {
  type ClipboardEvent,
  Component,
  type ComponentType,
  type DragEvent,
  type ErrorInfo,
  type ReactNode,
  type TouchEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useSearchParams } from "react-router";
import type { ChatHostProps } from "../shell/ChatHostSlot.tsx";
import { useProfileScope } from "../shell/ProfileScope.tsx";
import { useWideScreen } from "../shell/Sidebar.tsx";
import { Button } from "../ui/controls.tsx";
import { type AttachTokenKeeper, attachTokens } from "./attachToken.ts";
import {
  attachImage,
  ChatRail,
  type ChatRailProps,
  firstImage,
  storedWorkspace,
} from "./ChatRail.tsx";
import type { FeedDeps } from "./eventsFeed.ts";
import {
  browserPtyDeps,
  PtyConnection,
  type PtyDeps,
  type PtyRequest,
  type PtyStatus,
} from "./ptySocket.ts";
import { COPY } from "./reconnect.ts";
import {
  type ChatTerminal,
  CompositionGuard,
  createXtermTerminal,
  isMouseReport,
  type TerminalFactory,
} from "./terminal.ts";

/** Seams for tests; every default is the real browser piece. */
export interface ChatHostDeps {
  terminal?: TerminalFactory;
  pty?: PtyDeps;
  feed?: FeedDeps;
  tokens?: AttachTokenKeeper;
  rail?: ComponentType<ChatRailProps>;
}

export interface ChatHostFullProps extends ChatHostProps {
  deps?: ChatHostDeps;
}

const NOTICE_MS = 4_000;

/** A channel id for one mount of the host (ties the PTY child to the rail's feed). */
export function newChannelId(): string {
  const random =
    typeof globalThis.crypto?.randomUUID === "function"
      ? globalThis.crypto.randomUUID()
      : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  return `chat-${random}`;
}

class RailBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  override state = { failed: false };

  static getDerivedStateFromError(): { failed: boolean } {
    return { failed: true };
  }

  override componentDidCatch(error: Error, info: ErrorInfo): void {
    // biome-ignore lint/suspicious/noConsole: rail failures are logged, the terminal keeps going
    console.warn("[indus chat] the side panel failed", error, info.componentStack);
  }

  override render(): ReactNode {
    if (this.state.failed) {
      return (
        <p role="status" className="p-3 text-xs text-muted">
          Chat details are unavailable. The terminal is not affected.
        </p>
      );
    }
    return this.props.children;
  }
}

function Overlay({ children }: { children: ReactNode }) {
  return (
    <div
      data-testid="chat-overlay"
      className="absolute inset-0 flex items-center justify-center bg-bg/80 p-6"
    >
      <div
        role="alert"
        className="flex max-w-md flex-col items-start gap-3 rounded-lg border border-line bg-raised p-4 text-sm"
      >
        {children}
      </div>
    </div>
  );
}

function StatusLayer({
  status,
  onReconnect,
  onStartFresh,
}: {
  status: PtyStatus;
  onReconnect(): void;
  onStartFresh(): void;
}) {
  const stop = status.stop;
  if (status.phase === "reconnecting") {
    return (
      <p role="status" className="absolute inset-x-0 top-0 bg-warn/15 px-3 py-1 text-xs text-warn">
        {COPY.reconnecting}
      </p>
    );
  }
  if (!stop) return null;
  switch (stop.kind) {
    case "superseded":
      return (
        <p
          role="status"
          className="absolute inset-x-0 top-0 flex items-center gap-2 px-3 py-1 text-xs text-muted"
        >
          <span className="flex-1">{COPY.superseded}</span>
          <button type="button" className="underline" onClick={onReconnect}>
            Use it here
          </button>
        </p>
      );
    case "exited":
      return (
        <Overlay>
          <p>{COPY.exited}</p>
          <Button tone="primary" onClick={onStartFresh}>
            Start new session
          </Button>
        </Overlay>
      );
    case "start-failed":
      return (
        <Overlay>
          <p>{COPY.startFailed}</p>
          <Button onClick={onReconnect}>Try again</Button>
        </Overlay>
      );
    case "gave-up":
      return (
        <Overlay>
          <p>{COPY.gaveUp}</p>
          <Button tone="primary" onClick={onReconnect}>
            Reconnect
          </Button>
        </Overlay>
      );
    case "rejected":
      return (
        <Overlay>
          <p>{stop.message}</p>
          {stop.reload ? (
            <Button tone="primary" onClick={() => window.location.reload()}>
              Reload page
            </Button>
          ) : null}
        </Overlay>
      );
  }
}

/** Bottom inset that keeps the prompt above a mobile soft keyboard. */
function useKeyboardInset(): number {
  const [inset, setInset] = useState(0);
  useEffect(() => {
    const viewport = window.visualViewport;
    if (!viewport) return;
    const update = () => {
      const covered = window.innerHeight - viewport.height - viewport.offsetTop;
      setInset(covered > 1 ? Math.round(covered) : 0);
    };
    viewport.addEventListener("resize", update);
    viewport.addEventListener("scroll", update);
    return () => {
      viewport.removeEventListener("resize", update);
      viewport.removeEventListener("scroll", update);
    };
  }, []);
  return inset;
}

export function ChatHost({ active, deps = {} }: ChatHostFullProps) {
  const [channel] = useState(newChannelId);
  const [searchParams, setSearchParams] = useSearchParams();
  const { active: profile } = useProfileScope();
  const wide = useWideScreen();
  const inset = useKeyboardInset();

  const makeTerminal = deps.terminal ?? createXtermTerminal;
  const ptyDeps = deps.pty ?? browserPtyDeps;
  const tokens = deps.tokens ?? attachTokens;
  const Rail = deps.rail ?? ChatRail;

  const viewRef = useRef<HTMLDivElement | null>(null);
  const termRef = useRef<ChatTerminal | null>(null);
  const connRef = useRef<PtyConnection | null>(null);
  const activeRef = useRef(active);
  activeRef.current = active;
  const profileRef = useRef(profile);
  profileRef.current = profile;

  const [status, setStatus] = useState<PtyStatus>({ phase: "connecting" });
  const [resumeId, setResumeId] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [ready, setReady] = useState(false);
  const [railOpen, setRailOpen] = useState(false);

  const resumeParam = (searchParams.get("resume") ?? "").trim();
  const initialResume = useRef(resumeParam);

  const showNotice = useCallback((text: string) => setNotice(text), []);
  useEffect(() => {
    if (!notice) return;
    const timer = setTimeout(() => setNotice(null), NOTICE_MS);
    return () => clearTimeout(timer);
  }, [notice]);

  /** Dial for `resume` (or a fresh chat); `rotate` mints a new attach token first. */
  const start = useCallback(
    async (options: { resume?: string; fresh?: boolean; rotate?: boolean }) => {
      const conn = connRef.current;
      if (!conn) return;
      const attach = options.rotate ? await tokens.rotate() : await tokens.current();
      if (connRef.current !== conn) return;
      const request: PtyRequest = { channel, attach };
      const who = profileRef.current;
      if (who) request.profile = who;
      if (options.resume) request.resume = options.resume;
      else {
        const cwd = storedWorkspace(who);
        if (cwd) request.cwd = cwd;
        if (options.fresh) request.fresh = true;
      }
      setResumeId(options.resume ?? null);
      conn.connect(request);
    },
    [channel, tokens],
  );

  // Terminal + connection: made once per mount.
  useEffect(() => {
    let cancelled = false;
    let cleanups: Array<() => void> = [];
    void (async () => {
      const term = await makeTerminal();
      const view = viewRef.current;
      if (cancelled || !view) {
        term.dispose();
        return;
      }
      termRef.current = term;
      const conn = new PtyConnection(
        {
          output: (text) => term.write(text),
          status: setStatus,
          resumed: (id) => setResumeId(id),
          notice: showNotice,
        },
        () => term.size(),
        ptyDeps,
      );
      connRef.current = conn;
      const guard = new CompositionGuard((text) => conn.send(text));
      term.open(view);
      term.fit();
      const offData = term.onData((data) => {
        if (isMouseReport(data)) return;
        const out = guard.filter(data);
        if (out) conn.send(out);
      });
      const offResize = term.onResize(() => conn.sendSize());
      const input = term.input();
      const onCompositionEnd = (event: CompositionEvent) => guard.commit(event.data ?? "");
      input?.addEventListener("compositionend", onCompositionEnd);
      const observer =
        typeof ResizeObserver === "function" ? new ResizeObserver(() => term.fit()) : null;
      observer?.observe(view);
      cleanups = [
        offData,
        offResize,
        () => input?.removeEventListener("compositionend", onCompositionEnd),
        () => observer?.disconnect(),
        () => guard.dispose(),
        () => conn.dispose(),
        () => term.dispose(),
      ];
      conn.setActive(activeRef.current);
      await start(initialResume.current ? { resume: initialResume.current } : {});
      if (!cancelled) setReady(true);
    })();
    return () => {
      cancelled = true;
      for (const cleanup of cleanups) cleanup();
      termRef.current = null;
      connRef.current = null;
    };
  }, [makeTerminal, ptyDeps, showNotice, start]);

  // Shown or hidden by the shell: park redials, release the GPU renderer, refit on return.
  useEffect(() => {
    const conn = connRef.current;
    const term = termRef.current;
    if (!ready || !conn || !term) return;
    conn.setActive(active);
    term.setRendererVisible(active && document.visibilityState !== "hidden");
    if (active) {
      term.fit();
      conn.sendSize();
      term.focus();
    }
  }, [active, ready]);

  // Page-return signals: dial a parked connection, follow tab visibility for the renderer.
  useEffect(() => {
    if (!ready) return;
    const resumed = () => {
      termRef.current?.setRendererVisible(
        activeRef.current && document.visibilityState !== "hidden",
      );
      connRef.current?.pageResumed();
    };
    document.addEventListener("visibilitychange", resumed);
    window.addEventListener("focus", resumed);
    window.addEventListener("online", resumed);
    window.addEventListener("pageshow", resumed);
    return () => {
      document.removeEventListener("visibilitychange", resumed);
      window.removeEventListener("focus", resumed);
      window.removeEventListener("online", resumed);
      window.removeEventListener("pageshow", resumed);
    };
  }, [ready]);

  // A `/chat?resume=<id>` link (Sessions page) while the chat is on screen: resume in place.
  useEffect(() => {
    if (!ready || !active || !resumeParam) return;
    if (connRef.current?.request?.resume === resumeParam) return;
    void start({ resume: resumeParam });
  }, [ready, active, resumeParam, start]);

  // Another profile picked: the terminal follows it.
  const lastProfile = useRef(profile);
  useEffect(() => {
    if (!ready || lastProfile.current === profile) return;
    lastProfile.current = profile;
    void start({});
  }, [ready, profile, start]);

  const dropResumeParam = useCallback(() => {
    setSearchParams(
      (params) => {
        const next = new URLSearchParams(params);
        next.delete("resume");
        return next;
      },
      { replace: true },
    );
  }, [setSearchParams]);

  const startFresh = useCallback(() => {
    if (activeRef.current) dropResumeParam();
    void start({ fresh: true, rotate: true });
    setRailOpen(false);
  }, [dropResumeParam, start]);

  const resumeSession = useCallback(
    (id: string) => {
      if (activeRef.current) {
        setSearchParams(
          (params) => {
            const next = new URLSearchParams(params);
            next.set("resume", id);
            return next;
          },
          { replace: true },
        );
      }
      void start({ resume: id });
      setRailOpen(false);
    },
    [setSearchParams, start],
  );

  const typeIntoChat = useCallback((text: string) => connRef.current?.send(text) ?? false, []);

  const reconnect = useCallback(() => connRef.current?.reconnect(), []);

  const onPaste = (event: ClipboardEvent<HTMLElement>) => {
    const image = firstImage(event.clipboardData?.files);
    if (!image) return; // text paste: xterm handles it
    event.preventDefault();
    event.stopPropagation();
    attachImage(image, typeIntoChat).then(
      () => showNotice(`${image.name || "The image"} goes with your next message.`),
      (error: unknown) =>
        showNotice(error instanceof Error ? error.message : "The image could not be attached."),
    );
  };

  const onDrop = (event: DragEvent<HTMLElement>) => {
    const image = firstImage(event.dataTransfer?.files);
    if (!image) return;
    event.preventDefault();
    attachImage(image, typeIntoChat).then(
      () => showNotice(`${image.name || "The image"} goes with your next message.`),
      (error: unknown) =>
        showNotice(error instanceof Error ? error.message : "The image could not be attached."),
    );
  };

  // One-finger drags scroll the terminal on touch screens.
  const touchY = useRef<number | null>(null);
  const touchRest = useRef(0);
  const onTouchStart = (event: TouchEvent) => {
    touchY.current = event.touches.length === 1 ? (event.touches[0]?.clientY ?? null) : null;
    touchRest.current = 0;
  };
  const onTouchMove = (event: TouchEvent) => {
    const term = termRef.current;
    const y = event.touches[0]?.clientY;
    if (touchY.current === null || y === undefined || !term) return;
    const moved = touchY.current - y + touchRest.current;
    const height = term.rowHeight();
    const lines = Math.trunc(moved / height);
    touchY.current = y;
    touchRest.current = moved - lines * height;
    if (lines !== 0) term.scrollLines(lines);
  };

  const rail = useMemo(
    () => (
      <RailBoundary>
        <Rail
          channel={channel}
          profile={profile}
          connected={status.phase === "open"}
          resumeId={resumeId}
          onResume={resumeSession}
          onNewChat={startFresh}
          typeIntoChat={typeIntoChat}
          {...(deps.feed ? { feedDeps: deps.feed } : {})}
        />
      </RailBoundary>
    ),
    [
      Rail,
      channel,
      profile,
      status.phase,
      resumeId,
      resumeSession,
      startFresh,
      typeIntoChat,
      deps.feed,
    ],
  );

  return (
    <div className="flex min-h-0 flex-1" data-testid="chat-layout" data-phase={status.phase}>
      <section
        aria-label="Chat terminal"
        className="relative flex min-h-0 min-w-0 flex-1 flex-col bg-code p-2"
        style={inset ? { paddingBottom: inset } : undefined}
        onPasteCapture={onPaste}
        onDragOver={(event) => {
          if (Array.from(event.dataTransfer?.types ?? []).includes("Files")) event.preventDefault();
        }}
        onDrop={onDrop}
        onTouchStart={onTouchStart}
        onTouchMove={onTouchMove}
      >
        <div ref={viewRef} data-testid="chat-terminal" className="min-h-0 flex-1" />
        <StatusLayer status={status} onReconnect={reconnect} onStartFresh={startFresh} />
        {notice ? (
          <p
            role="status"
            data-testid="chat-notice"
            className="absolute bottom-3 left-3 right-3 rounded-md border border-line bg-raised px-3 py-1 text-xs"
          >
            {notice}
          </p>
        ) : null}
        {wide ? null : (
          <button
            type="button"
            className="absolute right-3 top-3 rounded-md border border-line bg-raised px-2 py-1 text-xs"
            aria-expanded={railOpen}
            onClick={() => setRailOpen(true)}
          >
            Details
          </button>
        )}
      </section>
      {wide ? (
        <aside aria-label="Chat details" className="w-72 shrink-0 border-l border-line bg-raised">
          {rail}
        </aside>
      ) : (
        <div
          className={railOpen ? "fixed inset-0 z-40 flex justify-end bg-bg/60" : "hidden"}
          aria-hidden={railOpen ? undefined : true}
        >
          <aside
            aria-label="Chat details"
            className="flex h-full w-80 max-w-[90vw] flex-col border-l border-line bg-raised"
          >
            <div className="flex justify-end p-2">
              <button
                type="button"
                className="rounded-md border border-line px-2 py-1 text-xs"
                onClick={() => setRailOpen(false)}
              >
                Close
              </button>
            </div>
            <div className="min-h-0 flex-1">{rail}</div>
          </aside>
        </div>
      )}
    </div>
  );
}

export default ChatHost;
