// The admin console modal (P24-36; W52-f look): a large panel with a real terminal (xterm) whose
// prompt line is the input — Enter runs, Ctrl+C cancels, the confirm step asks "[y/N]". It talks
// to `/api/console` (purpose `web-console`) through the shared socket helper, so the credential
// rides the subprotocol list and never the URL. Frames are JSON both ways; the server decides everything (which commands exist, what needs
// confirmation, the limits), the modal only shows it.

import * as RadixDialog from "@radix-ui/react-dialog";
import { SquareTerminal, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { activeProfile } from "../lib/api.ts";
import { openDashboardSocket } from "../lib/wsUrl.ts";
import { Badge, type BadgeTone, Button, IconButton } from "../ui/controls.tsx";
import { type ConsolePhase, ConsoleSession, type ConsoleTerminal } from "./consoleSession.ts";
import { type ConsoleTerminalFactory, createConsoleTerminal } from "./consoleTerminal.ts";

export const CONSOLE_PATH = "/api/console";
export const CONSOLE_PURPOSE = "web-console";

/** The part of a WebSocket the modal uses (a fake in tests). */
export interface ConsoleSocket {
  send(data: string): void;
  close(code?: number): void;
  onmessage: ((event: { data: unknown }) => void) | null;
  onclose: ((event: { code: number; reason?: string }) => void) | null;
  onerror: ((event: unknown) => void) | null;
}

export type ConnectConsole = (profile: string) => Promise<ConsoleSocket>;

/** The real dial: the shared helper adds the credential as a subprotocol. */
export const connectConsole: ConnectConsole = async (profile) =>
  (await openDashboardSocket(CONSOLE_PATH, CONSOLE_PURPOSE, {
    profile: profile || undefined,
  })) as unknown as ConsoleSocket;

const CLOSE_TEXT: Readonly<Record<number, string>> = {
  1000: "The console closed.",
  4401: "The console needs you to be signed in.",
  4403: "The console refused this page's origin.",
  4404: "The admin console is not enabled on this dashboard.",
  4408: "The console only answers this machine.",
  4400: "There is no such profile.",
  1011: "The console could not start.",
};

const PHASE_TONE: Record<ConsolePhase, BadgeTone> = {
  connecting: "neutral",
  ready: "ok",
  running: "warn",
  closed: "danger",
  error: "danger",
};

function textOf(value: unknown): string {
  return typeof value === "string" ? value : "";
}

export function ConsoleModal({
  open,
  onClose,
  connect = connectConsole,
  createTerminal = createConsoleTerminal,
}: {
  open: boolean;
  onClose: () => void;
  connect?: ConnectConsole;
  createTerminal?: ConsoleTerminalFactory;
}) {
  const [, setTick] = useState(0);
  const [attempt, setAttempt] = useState(0);
  const [host, setHost] = useState<HTMLDivElement | null>(null);
  const socket = useRef<ConsoleSocket | null>(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  const session = useMemo(
    () =>
      new ConsoleSession(
        (frame) => socket.current?.send(JSON.stringify(frame)),
        () => setTick((n) => n + 1),
        () => closeRef.current(),
      ),
    [],
  );

  // The screen: created once the panel's body is in the page, fitted to it on every resize.
  useEffect(() => {
    if (!open || !host) return;
    let live = true;
    let term: ConsoleTerminal | null = null;
    let unsubscribe: (() => void) | null = null;
    let observer: ResizeObserver | null = null;
    createTerminal()
      .then((made) => {
        if (!live) {
          made.dispose();
          return;
        }
        term = made;
        made.open(host);
        made.fit();
        session.attach(made);
        unsubscribe = made.onData((data) => session.input(data));
        made.focus();
        if (typeof ResizeObserver === "function") {
          observer = new ResizeObserver(() => made.fit());
          observer.observe(host);
        }
      })
      .catch(() => session.fail("The terminal could not start in this browser.", "error"));
    return () => {
      live = false;
      observer?.disconnect();
      unsubscribe?.();
      term?.dispose();
    };
  }, [open, host, createTerminal, session]);

  // The connection: dialled on open and on every Reconnect.
  // biome-ignore lint/correctness/useExhaustiveDependencies: `attempt` re-dials on Reconnect
  useEffect(() => {
    if (!open) return;
    let live = true;
    session.reset();
    connect(activeProfile())
      .then((ws) => {
        if (!live) {
          ws.close(1000);
          return;
        }
        socket.current = ws;
        ws.onmessage = (event) => {
          try {
            const frame: unknown = JSON.parse(textOf(event.data));
            if (frame && typeof frame === "object") session.frame(frame as Record<string, unknown>);
          } catch {
            // the server only sends JSON; anything else is ignored
          }
        };
        ws.onclose = (event) => {
          if (!live) return;
          socket.current = null;
          session.fail(CLOSE_TEXT[event.code] ?? `The console closed (${event.code}).`);
        };
        ws.onerror = () => undefined;
      })
      .catch((error: unknown) => {
        if (!live) return;
        session.fail(
          error instanceof Error ? error.message : "The console could not connect.",
          "error",
        );
      });
    return () => {
      live = false;
      socket.current?.close(1000);
      socket.current = null;
    };
  }, [open, connect, session, attempt]);

  const phase = session.phase;
  return (
    <RadixDialog.Root
      open={open}
      onOpenChange={(next) => {
        if (!next) onClose();
      }}
    >
      <RadixDialog.Portal>
        <RadixDialog.Overlay className="fixed inset-0 z-40 bg-canvas/85 backdrop-blur-[1px] [animation:indus-fade-in_160ms_ease-out]" />
        <RadixDialog.Content
          onOpenAutoFocus={(event) => event.preventDefault()}
          className="fixed top-1/2 left-1/2 z-50 flex h-[min(82vh,47.5rem)] w-[min(60rem,94vw)] -translate-x-1/2 -translate-y-1/2 flex-col border border-line-strong bg-surface text-ink shadow-2xl [animation:indus-pop-in_180ms_ease-out]"
        >
          <div className="flex h-[4.5rem] shrink-0 items-center gap-3 border-b border-line-strong px-4 py-3">
            <span className="inline-flex size-[2.125rem] shrink-0 items-center justify-center border border-line-strong bg-canvas">
              <SquareTerminal aria-hidden="true" className="size-4 text-ink" />
            </span>
            <div className="flex min-w-0 flex-1 flex-col gap-1">
              <RadixDialog.Title className="type-serif m-0 text-[1rem] leading-tight tracking-[0.03em] text-ink uppercase">
                Indus console
              </RadixDialog.Title>
              <RadixDialog.Description asChild>
                <div className="flex items-center gap-2">
                  <Badge tone={PHASE_TONE[phase]}>{phase}</Badge>
                  {session.profile ? (
                    <span className="font-mono text-[0.75rem] text-ink-muted">
                      {session.profile}
                    </span>
                  ) : null}
                  {phase === "closed" || phase === "error" ? (
                    <Button size="sm" className="h-6" onClick={() => setAttempt((n) => n + 1)}>
                      Reconnect
                    </Button>
                  ) : null}
                </div>
              </RadixDialog.Description>
            </div>
            <RadixDialog.Close asChild>
              <IconButton label="Close console">
                <X />
              </IconButton>
            </RadixDialog.Close>
          </div>
          <div className="min-h-0 flex-1 bg-black p-2">
            <div
              ref={setHost}
              data-testid="console-screen"
              // The terminal's own colour under the rows: the fit leaves up to one row of space at
              // the bottom, which showed as a black strip on light (W54-W1-7).
              className="h-full w-full bg-terminal"
              data-phase={phase}
            />
          </div>
        </RadixDialog.Content>
      </RadixDialog.Portal>
    </RadixDialog.Root>
  );
}

/** The shell's entry: a button that opens the modal (only when the backend serves the console). */
export function ConsoleEntry({
  enabled,
  compact = false,
}: {
  enabled: boolean;
  compact?: boolean;
}) {
  const [open, setOpen] = useState(false);
  if (!enabled) return null;
  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        aria-label={compact ? "Admin console" : undefined}
        title={compact ? "Admin console" : undefined}
        className={`flex h-8 items-center gap-3 text-[0.75rem] tracking-[0.06em] text-ink-muted hover:text-ink ${compact ? "justify-center" : "px-5"}`}
      >
        <SquareTerminal aria-hidden="true" className="size-3.5 shrink-0" />
        {compact ? null : "Admin console"}
      </button>
      {open ? <ConsoleModal open onClose={() => setOpen(false)} /> : null}
    </>
  );
}
