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

import { useQuery } from "@tanstack/react-query";
import { Copy, PanelRight, X } from "lucide-react";
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
import { api } from "../lib/api.ts";
import type { ChatHostProps } from "../shell/ChatHostSlot.tsx";
import { HeaderActions, HeaderTitle } from "../shell/PageHeader.tsx";
import { useProfileScope } from "../shell/ProfileScope.tsx";
import { useWideScreen } from "../shell/Sidebar.tsx";
import { Button, IconButton } from "../ui/controls.tsx";
import { cx } from "../ui/cx.ts";
import { type AttachTokenKeeper, attachTokens } from "./attachToken.ts";
import {
  attachImage,
  ChatRail,
  type ChatRailProps,
  firstImage,
  storedWorkspace,
  useEventsFeed,
} from "./ChatRail.tsx";
import { browserFeedDeps, type FeedDeps } from "./eventsFeed.ts";
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
const COPIED_MS = 1_500;

/** Where the side panel's collapsed state is kept, per browser (W52-a). */
export const PANEL_COLLAPSED_KEY = "indus.chat.panelCollapsed";

function readCollapsed(): boolean {
  try {
    return window.localStorage.getItem(PANEL_COLLAPSED_KEY) === "1";
  } catch {
    return false;
  }
}

function writeCollapsed(collapsed: boolean): void {
  try {
    if (collapsed) window.localStorage.setItem(PANEL_COLLAPSED_KEY, "1");
    else window.localStorage.removeItem(PANEL_COLLAPSED_KEY);
  } catch {
    // storage blocked: the choice lasts for this page only
  }
}

/** The small see-through buttons floating on the terminal card ("copy last response", "panel"). */
const floatingButton =
  "inline-flex h-7 items-center gap-1.5 rounded-[4px] border border-terminal-fg/30 bg-black/20 px-2.5 font-mono text-[0.75rem] text-terminal-fg opacity-70 transition-opacity hover:opacity-100 disabled:cursor-not-allowed disabled:opacity-40 [&_svg]:size-3";

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
      className="absolute inset-0 z-10 flex items-center justify-center rounded-lg bg-black/60 p-6"
    >
      <div
        role="alert"
        className="flex max-w-md flex-col items-center gap-3 text-center font-mono text-[0.8125rem] text-[#f0e6d2]"
      >
        {children}
      </div>
    </div>
  );
}

/** The amber box at the card's top right while the terminal redials or was taken over. */
function Corner({ children }: { children: ReactNode }) {
  return (
    <div
      role="status"
      className="absolute top-4 right-4 z-10 flex max-w-sm items-center gap-3 border border-warning/60 bg-black/80 px-3 py-2 font-mono text-[0.75rem] text-warning"
    >
      {children}
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
      <Corner>
        <span className="flex-1">{COPY.reconnecting}</span>
        <Button size="sm" className="h-6 border-warning text-warning" onClick={onReconnect}>
          Reconnect now
        </Button>
      </Corner>
    );
  }
  if (!stop) return null;
  switch (stop.kind) {
    case "superseded":
      return (
        <Corner>
          <span className="flex-1">{COPY.superseded}</span>
          <Button size="sm" className="h-6 border-warning text-warning" onClick={onReconnect}>
            Use it here
          </Button>
        </Corner>
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
  const [collapsed, setCollapsed] = useState(readCollapsed);
  const [copied, setCopied] = useState(false);
  const feed = useEventsFeed(channel, deps.feed ?? browserFeedDeps);
  // The header shows the session's title: from the feed when it says so, else from the store
  // (looked up again after each answer, when the server may have named the session). A fresh
  // chat is not in the store until its first answer, so it is not looked up before then.
  const liveId = feed.live?.sessionId;
  const stored = useQuery({
    queryKey: ["chat", "session-title", liveId, feed.lastAnswer],
    queryFn: () => api.sessions.one(liveId ?? ""),
    enabled: active && !!liveId && !feed.live?.title && (!!feed.lastAnswer || liveId === resumeId),
    retry: false,
    staleTime: 30_000,
  });
  const headerTitle = feed.live?.title || (liveId ? stored.data?.title : null) || null;

  const collapse = useCallback((next: boolean) => {
    setCollapsed(next);
    writeCollapsed(next);
  }, []);

  useEffect(() => {
    if (!copied) return;
    const timer = setTimeout(() => setCopied(false), COPIED_MS);
    return () => clearTimeout(timer);
  }, [copied]);

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

  const copyLast = async () => {
    if (!feed.lastAnswer) return;
    try {
      await navigator.clipboard.writeText(feed.lastAnswer);
      setCopied(true);
      showNotice("Last answer copied.");
    } catch {
      showNotice("The browser did not allow copying.");
    }
  };

  const railFor = useCallback(
    (onCollapse?: () => void) => (
      <RailBoundary>
        <Rail
          channel={channel}
          profile={profile}
          connected={status.phase === "open"}
          resumeId={resumeId}
          onResume={resumeSession}
          onNewChat={startFresh}
          typeIntoChat={typeIntoChat}
          feed={feed}
          {...(onCollapse ? { onCollapse } : {})}
        />
      </RailBoundary>
    ),
    [Rail, channel, profile, status.phase, resumeId, resumeSession, startFresh, typeIntoChat, feed],
  );
  const wideRail = useMemo(() => railFor(() => collapse(true)), [railFor, collapse]);
  const drawerRail = useMemo(() => railFor(), [railFor]);
  const panelShown = wide && !collapsed;

  return (
    <div
      className="flex min-h-0 flex-1 gap-3 px-2 pt-2 lg:pt-[0.9375rem] lg:pr-[1.6875rem] lg:pl-[1.4375rem]"
      data-testid="chat-layout"
      data-phase={status.phase}
    >
      <HeaderTitle title={active ? headerTitle : null} />
      {active && !wide ? (
        <HeaderActions>
          <button
            type="button"
            aria-expanded={railOpen}
            onClick={() => setRailOpen(true)}
            className="type-button inline-flex h-7 items-center gap-1.5 border border-ink/20 px-2 text-[0.75rem] text-ink hover:bg-hover [&_svg]:size-3"
          >
            <PanelRight aria-hidden="true" />
            Model &amp; tools
          </button>
        </HeaderActions>
      ) : null}
      <section
        aria-label="Chat terminal"
        className="relative flex min-h-0 min-w-0 flex-1 flex-col rounded-lg bg-terminal p-2 shadow-[0_8px_32px_rgb(0_0_0/0.4)] lg:p-3"
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
            className="absolute bottom-14 left-4 z-10 max-w-[70%] border border-terminal-fg/30 bg-black/70 px-3 py-1 font-mono text-[0.75rem] text-[#f0e6d2]"
          >
            {notice}
          </p>
        ) : null}
        <button
          type="button"
          aria-label="Copy last answer"
          title={feed.lastAnswer ? "Copy the last answer" : "No answer in this chat yet"}
          disabled={!feed.lastAnswer}
          onClick={() => void copyLast()}
          className={cx(floatingButton, "absolute right-2 bottom-2 z-10 lg:right-4 lg:bottom-4")}
        >
          <Copy aria-hidden="true" />
          <span className="max-[400px]:hidden">{copied ? "copied" : "copy last response"}</span>
        </button>
        {wide && collapsed ? (
          <button
            type="button"
            aria-label="Show side panel"
            onClick={() => collapse(false)}
            className={cx(floatingButton, "absolute top-2.5 right-3 z-10 h-6")}
          >
            <PanelRight aria-hidden="true" />
            panel
          </button>
        ) : null}
      </section>
      {wide ? (
        <aside aria-label="Chat details" hidden={!panelShown} className="w-[15rem] shrink-0 pb-3">
          {wideRail}
        </aside>
      ) : (
        <div
          className={railOpen ? "fixed inset-0 z-40 flex justify-end bg-overlay" : "hidden"}
          aria-hidden={railOpen ? undefined : true}
        >
          <aside
            aria-label="Chat details"
            className="flex h-full w-80 max-w-[90vw] flex-col gap-2 border-l border-line bg-canvas p-3 [animation:indus-slide-in_160ms_ease-out]"
          >
            <div className="flex justify-end">
              <IconButton label="Close" onClick={() => setRailOpen(false)}>
                <X />
              </IconButton>
            </div>
            <div className="min-h-0 flex-1">{drawerRail}</div>
          </aside>
        </div>
      )}
    </div>
  );
}

export default ChatHost;
