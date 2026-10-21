// The terminal pane (P20-31): a rail of terminals, each a shell main runs in a PTY. A terminal that
// is not in front — or the whole pane when the shell hides it — stays mounted, so its screen and
// its shell (same pid) survive. Closing a running terminal asks first. "Add selection to chat"
// hands the selected text to the composer.

import { Plus, SquareTerminal, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { desktopBridge } from "../../app/bridge.ts";
import { Loader } from "../../app/Loader.tsx";
import type { IndusDesktopBridge } from "../../global.d.ts";
import { useT } from "../../i18n/index.tsx";
import { createXtermView, type TerminalView, type TerminalViewFactory } from "./xtermView.ts";

export type TerminalBridge = Pick<IndusDesktopBridge, "terminal">;

export interface TerminalPaneProps {
  /** The folder new shells start in (main falls back to home outside the window's folders). */
  cwd: string | null;
  mode: "local" | "remote" | null;
  hidden?: boolean;
  bridge?: TerminalBridge | null;
  createView?: TerminalViewFactory;
  onAddToChat?(text: string): void;
}

interface Slot {
  key: number;
  label: string;
}

interface SlotState {
  terminalId: string | null;
  pid: number | null;
  shell: string | null;
  exitCode: number | null | undefined;
  error: string | null;
}

const BUTTON =
  "rounded border border-line px-2 py-0.5 text-xs hover:bg-raised focus-visible:outline-2 focus-visible:outline-focus";

function errorText(error: unknown): string {
  const text = error instanceof Error ? error.message : String(error);
  const at = text.indexOf(": ");
  return at > 0 && /^[a-z-]+$/.test(text.slice(0, at)) ? text.slice(at + 2) : text;
}

/** One terminal: its screen, its shell, its events. */
function TerminalSlot(props: {
  active: boolean;
  visible: boolean;
  cwd: string | null;
  bridge: TerminalBridge;
  createView: TerminalViewFactory;
  restartKey: number;
  onState(state: SlotState): void;
  onSelection(text: string): void;
}) {
  const host = useRef<HTMLDivElement | null>(null);
  const viewRef = useRef<TerminalView | null>(null);
  const idRef = useRef<string | null>(null);
  const { bridge, createView, cwd, restartKey, onState, onSelection } = props;

  // biome-ignore lint/correctness/useExhaustiveDependencies: restartKey starts a fresh shell (Restart)
  useEffect(() => {
    const element = host.current;
    if (element === null) return;
    let live = true;
    let view: TerminalView | null = null;
    let shellName: string | null = null;
    // Output that arrives before `start` answers is kept and replayed for our id.
    const early: Array<{ terminalId: string; data: string }> = [];
    const offData = bridge.terminal.onData((event) => {
      if (idRef.current === null) early.push(event);
      else if (event.terminalId === idRef.current) view?.write(event.data);
    });
    const offExit = bridge.terminal.onExit((event) => {
      if (event.terminalId !== idRef.current) return;
      onState({
        terminalId: event.terminalId,
        pid: null,
        shell: shellName,
        exitCode: event.exitCode,
        error: null,
      });
    });
    (async () => {
      try {
        view = await createView();
        if (!live) {
          view.dispose();
          return;
        }
        viewRef.current = view;
        view.open(element);
        const size = view.fit();
        view.onInput((data) => {
          const id = idRef.current;
          if (id !== null) bridge.terminal.write({ terminalId: id, data });
        });
        view.onSelectionChange(onSelection);
        const started = await bridge.terminal.start({
          ...(cwd === null ? {} : { cwd }),
          cols: Math.max(2, size.cols),
          rows: Math.max(2, size.rows),
        });
        if (!live) {
          void bridge.terminal.dispose({ terminalId: started.terminalId }).catch(() => {});
          return;
        }
        idRef.current = started.terminalId;
        shellName = started.shell;
        for (const event of early.splice(0))
          if (event.terminalId === started.terminalId) view.write(event.data);
        onState({
          terminalId: started.terminalId,
          pid: started.pid,
          shell: started.shell,
          exitCode: undefined,
          error: null,
        });
      } catch (error) {
        if (live)
          onState({
            terminalId: null,
            pid: null,
            shell: null,
            exitCode: undefined,
            error: errorText(error),
          });
      }
    })();
    return () => {
      live = false;
      offData();
      offExit();
      const id = idRef.current;
      idRef.current = null;
      if (id !== null) void bridge.terminal.dispose({ terminalId: id }).catch(() => {});
      view?.dispose();
      viewRef.current = null;
    };
  }, [bridge, createView, cwd, restartKey, onState, onSelection]);

  // Shown again (tab switch or the pane unhidden): refit and tell the PTY.
  useEffect(() => {
    if (!props.active || !props.visible) return;
    const view = viewRef.current;
    const id = idRef.current;
    if (view === null || id === null) return;
    const size = view.fit();
    bridge.terminal.resize({
      terminalId: id,
      cols: Math.max(2, size.cols),
      rows: Math.max(2, size.rows),
    });
    view.focus();
  }, [props.active, props.visible, bridge]);

  useEffect(() => {
    const element = host.current;
    if (element === null || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      const view = viewRef.current;
      const id = idRef.current;
      if (view === null || id === null || !props.active || !props.visible) return;
      const size = view.fit();
      bridge.terminal.resize({
        terminalId: id,
        cols: Math.max(2, size.cols),
        rows: Math.max(2, size.rows),
      });
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, [bridge, props.active, props.visible]);

  return (
    <div
      ref={host}
      className={`min-h-0 flex-1 bg-code p-1 ${props.active ? "" : "hidden"}`}
      data-testid="terminal-screen"
    />
  );
}

export function TerminalPane(props: TerminalPaneProps) {
  const t = useT();
  const bridge =
    props.bridge === undefined ? (desktopBridge() as TerminalBridge | null) : props.bridge;
  const createView = props.createView ?? createXtermView;
  const nextKey = useRef(1);
  const [slots, setSlots] = useState<Slot[]>(() => [{ key: 0, label: "1" }]);
  const [active, setActive] = useState(0);
  const [states, setStates] = useState<Record<number, SlotState>>({});
  const [restarts, setRestarts] = useState<Record<number, number>>({});
  const [closing, setClosing] = useState<number | null>(null);
  const [selection, setSelection] = useState("");

  const stateHandlers = useRef(new Map<number, (state: SlotState) => void>());
  const handlerFor = useCallback((key: number) => {
    let handler = stateHandlers.current.get(key);
    if (handler === undefined) {
      handler = (state: SlotState) => setStates((prev) => ({ ...prev, [key]: state }));
      stateHandlers.current.set(key, handler);
    }
    return handler;
  }, []);

  if (props.mode === "remote")
    return <p className="p-3 text-sm text-muted">{t("terminal.remote")}</p>;
  if (bridge === null) return <p className="p-3 text-sm text-muted">{t("pane.unavailable")}</p>;

  const add = () => {
    const key = nextKey.current++;
    setSlots((prev) => [...prev, { key, label: String(key + 1) }]);
    setActive(key);
  };

  const remove = (key: number) => {
    setClosing(null);
    const rest = slots.filter((slot) => slot.key !== key);
    setSlots(rest);
    if (active === key) setActive(rest.at(-1)?.key ?? -1);
    setStates((prev) => {
      const { [key]: _gone, ...others } = prev;
      return others;
    });
  };

  const askClose = (key: number) => {
    const state = states[key];
    const running =
      state?.terminalId !== null && state?.terminalId !== undefined && state.exitCode === undefined;
    if (running) setClosing(key);
    else remove(key);
  };

  const current = states[active];
  const visible = props.hidden !== true;
  return (
    <section
      className={`flex h-full min-h-0 flex-col ${visible ? "" : "invisible"}`}
      aria-label={t("pane.terminal")}
      inert={!visible}
      data-testid="terminal-pane"
    >
      <div
        className="flex items-center gap-1 border-b border-line px-2 py-1"
        role="tablist"
        aria-label={t("terminal.list")}
      >
        {slots.map((slot) => (
          <span key={slot.key} className="flex items-center">
            <button
              type="button"
              role="tab"
              aria-selected={slot.key === active}
              className={`flex items-center gap-1 rounded px-2 py-0.5 text-xs ${slot.key === active ? "bg-raised font-medium" : "hover:bg-raised"}`}
              onClick={() => setActive(slot.key)}
            >
              <SquareTerminal size={12} aria-hidden="true" />
              {states[slot.key]?.shell ?? slot.label}
            </button>
            <button
              type="button"
              className="rounded p-0.5 text-muted hover:text-fg"
              aria-label={t("terminal.close", { name: states[slot.key]?.shell ?? slot.label })}
              onClick={() => askClose(slot.key)}
            >
              <X size={12} aria-hidden="true" />
            </button>
          </span>
        ))}
        <button
          type="button"
          className="rounded p-1 hover:bg-raised"
          aria-label={t("terminal.new")}
          onClick={add}
        >
          <Plus size={12} aria-hidden="true" />
        </button>
        <span className="flex-1" />
        {props.onAddToChat !== undefined && selection.trim() !== "" ? (
          <button type="button" className={BUTTON} onClick={() => props.onAddToChat?.(selection)}>
            {t("terminal.addSelection")}
          </button>
        ) : null}
      </div>

      {closing !== null ? (
        <div
          role="alertdialog"
          aria-label={t("pane.terminal")}
          className="flex items-center gap-2 border-b border-line px-3 py-1 text-xs"
        >
          <span className="flex-1">{t("terminal.confirmClose")}</span>
          <button type="button" className={BUTTON} onClick={() => remove(closing)}>
            {t("terminal.confirm")}
          </button>
          <button type="button" className={BUTTON} onClick={() => setClosing(null)}>
            {t("terminal.keep")}
          </button>
        </div>
      ) : null}

      {current?.error ? (
        <p role="alert" className="px-3 py-2 text-sm text-danger">
          {t("terminal.unavailable", { message: current.error })}
        </p>
      ) : null}
      {current !== undefined && current.exitCode !== undefined && current.error === null ? (
        <p role="status" className="flex items-center gap-2 px-3 py-1 text-xs">
          <span>{t("terminal.exited", { code: current.exitCode ?? "?" })}</span>
          <button
            type="button"
            className={BUTTON}
            onClick={() => setRestarts((prev) => ({ ...prev, [active]: (prev[active] ?? 0) + 1 }))}
          >
            {t("terminal.restart")}
          </button>
        </p>
      ) : null}
      {current === undefined && slots.length > 0 ? (
        <Loader label={t("terminal.starting")} size="sm" />
      ) : null}

      <div className="flex min-h-0 flex-1 flex-col">
        {slots.map((slot) => (
          <TerminalSlot
            key={slot.key}
            active={slot.key === active}
            visible={visible}
            cwd={props.cwd}
            bridge={bridge}
            createView={createView}
            restartKey={restarts[slot.key] ?? 0}
            onState={handlerFor(slot.key)}
            onSelection={setSelection}
          />
        ))}
      </div>
    </section>
  );
}
