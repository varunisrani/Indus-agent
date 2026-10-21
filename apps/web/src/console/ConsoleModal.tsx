// The admin console modal (P24-36): one line of input, a log of what came back, and the confirm
// step for commands that change something. It talks to `/api/console` (purpose `web-console`)
// through the shared socket helper, so the credential rides the subprotocol list and never the
// URL. Frames are JSON both ways; the server decides everything (which commands exist, what needs
// confirmation, the limits), the modal only shows it.

import { type FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { activeProfile } from "../lib/api.ts";
import { openDashboardSocket } from "../lib/wsUrl.ts";
import { Button } from "../ui/controls.tsx";
import { Modal } from "../ui/Dialog.tsx";

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

interface LogLine {
  key: number;
  kind: "input" | "stdout" | "stderr" | "error" | "note";
  text: string;
}

interface Pending {
  command: string;
  message: string;
}

type Phase = "connecting" | "ready" | "running" | "closed";

const CLOSE_TEXT: Readonly<Record<number, string>> = {
  1000: "The console closed.",
  4401: "The console needs you to be signed in.",
  4403: "The console refused this page's origin.",
  4404: "The admin console is not enabled on this dashboard.",
  4408: "The console only answers this machine.",
  4400: "There is no such profile.",
  1011: "The console could not start.",
};

function textOf(value: unknown): string {
  return typeof value === "string" ? value : "";
}

export function ConsoleModal({
  open,
  onClose,
  connect = connectConsole,
}: {
  open: boolean;
  onClose: () => void;
  connect?: ConnectConsole;
}) {
  const [lines, setLines] = useState<LogLine[]>([]);
  const [phase, setPhase] = useState<Phase>("connecting");
  const [prompt, setPrompt] = useState("indus> ");
  const [profile, setProfile] = useState("");
  const [pending, setPending] = useState<Pending | null>(null);
  const [draft, setDraft] = useState("");
  const socket = useRef<ConsoleSocket | null>(null);
  const counter = useRef(0);

  const add = useCallback((kind: LogLine["kind"], text: string) => {
    if (!text) return;
    counter.current += 1;
    const key = counter.current;
    setLines((was) => [...was, { key, kind, text }]);
  }, []);

  const handle = useCallback(
    (frame: Record<string, unknown>) => {
      switch (frame.type) {
        case "ready":
          setPhase("ready");
          setProfile(textOf(frame.profile));
          if (typeof frame.prompt === "string") setPrompt(frame.prompt);
          return;
        case "output":
          add(frame.stream === "stderr" ? "stderr" : "stdout", textOf(frame.data));
          return;
        case "error":
          add("error", textOf(frame.message));
          return;
        case "confirm_required":
          setPending({ command: textOf(frame.command), message: textOf(frame.message) });
          return;
        case "clear":
          setLines([]);
          return;
        case "complete":
          if (typeof frame.prompt === "string") setPrompt(frame.prompt);
          if (frame.status === "exit") {
            setPhase("closed");
            onClose();
            return;
          }
          if (frame.status === "cancelled") {
            setPending(null);
            add("note", "Cancelled.");
          }
          if (frame.status !== "confirm_required") setPending(null);
          setPhase("ready");
          return;
        default:
          return;
      }
    },
    [add, onClose],
  );

  useEffect(() => {
    if (!open) return;
    let live = true;
    setLines([]);
    setPending(null);
    setPhase("connecting");
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
            if (frame && typeof frame === "object") handle(frame as Record<string, unknown>);
          } catch {
            // the server only sends JSON; anything else is ignored
          }
        };
        ws.onclose = (event) => {
          if (!live) return;
          socket.current = null;
          setPhase("closed");
          add("note", CLOSE_TEXT[event.code] ?? `The console closed (${event.code}).`);
        };
        ws.onerror = () => undefined;
      })
      .catch((error: unknown) => {
        if (!live) return;
        setPhase("closed");
        add("error", error instanceof Error ? error.message : "The console could not connect.");
      });
    return () => {
      live = false;
      socket.current?.close(1000);
      socket.current = null;
    };
  }, [open, connect, handle, add]);

  const send = (frame: Record<string, unknown>) => socket.current?.send(JSON.stringify(frame));

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const line = draft.trim();
    if (!line || phase !== "ready" || pending !== null) return;
    add("input", `${prompt}${line}`);
    setDraft("");
    setPhase("running");
    send({ type: "input", line });
  };

  return (
    <Modal
      open={open}
      onOpenChange={(next) => {
        if (!next) onClose();
      }}
      title="Admin console"
      description={
        profile
          ? `Curated indus commands for ${profile}, one per line. Type help for the list.`
          : "Curated indus commands, one per line. Type help for the list."
      }
    >
      <div
        role="log"
        aria-live="polite"
        aria-label="Console output"
        className="mb-3 h-64 overflow-auto rounded-md border border-line bg-code p-2 font-mono text-xs whitespace-pre-wrap"
      >
        {lines.map((line) => (
          <div
            key={line.key}
            data-kind={line.kind}
            className={
              line.kind === "error" || line.kind === "stderr"
                ? "text-danger"
                : line.kind === "note"
                  ? "text-muted"
                  : ""
            }
          >
            {line.text}
          </div>
        ))}
      </div>
      {pending !== null ? (
        <div role="alertdialog" aria-label="Confirm command" className="mb-3 text-sm">
          <p className="mb-2">
            {pending.message} <code>{pending.command}</code>
          </p>
          <span className="inline-flex gap-2">
            <Button
              tone="primary"
              onClick={() => send({ type: "confirm", command: pending.command })}
            >
              Run it
            </Button>
            <Button onClick={() => send({ type: "cancel" })}>Cancel</Button>
          </span>
        </div>
      ) : null}
      <form onSubmit={submit} className="flex items-center gap-2">
        <label className="flex flex-1 items-center gap-1 font-mono text-sm">
          <span aria-hidden="true">{prompt}</span>
          <input
            aria-label="Console command"
            className="flex-1 rounded-md border border-line bg-bg px-2 py-1"
            value={draft}
            disabled={phase !== "ready" || pending !== null}
            onChange={(event) => setDraft(event.target.value)}
            autoComplete="off"
            spellCheck={false}
          />
        </label>
        {phase === "running" ? (
          <Button tone="danger" onClick={() => send({ type: "cancel" })}>
            Stop
          </Button>
        ) : (
          <Button type="submit" disabled={phase !== "ready" || pending !== null}>
            Run
          </Button>
        )}
      </form>
    </Modal>
  );
}

/** The shell's entry: a button that opens the modal (only when the backend serves the console). */
export function ConsoleEntry({ enabled }: { enabled: boolean }) {
  const [open, setOpen] = useState(false);
  if (!enabled) return null;
  return (
    <>
      <Button tone="ghost" className="w-full justify-start" onClick={() => setOpen(true)}>
        Admin console
      </Button>
      {open ? <ConsoleModal open onClose={() => setOpen(false)} /> : null}
    </>
  );
}
