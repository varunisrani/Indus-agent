// The wake word on the desktop (P24-20), three pieces:
//
// - `WakeIndicator`: the root of the indicator window (`?win=wake`), a small light with a word
//   that says what the microphone is doing (listening / heard / recording). Main shows the
//   window whenever the state is not `hidden`.
// - `WakeController`: the app window's side. `start({ consent })` asks the daemon (`wake.start`,
//   waiting up to `WAKE_START_TIMEOUT_MS` because a first start may load a model), `stop`, and
//   follows `wake.detected`: the light turns to "heard", the hand-off callback starts voice mode
//   (P24-35 owns capture), and `captureDone()` resumes listening. The light follows every step,
//   so the microphone is never in use without it.
// - `WakeToggle`: the Settings switch. Only this explicit switch sends `consent: true`.
//
// `wake.*` are not in the generated method catalogue yet (P24-20 wiring): calls go through
// `LooseRpc`.

import { useEffect, useState, useSyncExternalStore } from "react";
import { desktopBridge } from "../app/bridge.ts";
import type { IndusWakeIndicatorState } from "../global.d.ts";
import { type MessageKey, useT } from "../i18n/index.tsx";
import type { LooseRpc } from "../pet/PetOverlay.tsx";

export const WAKE_START_TIMEOUT_MS = 180_000;
export const DEFAULT_PHRASE = "hey indus";

export interface WakeTimers {
  setTimeout(fn: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
}

export interface WakeView {
  readonly enabled: boolean;
  readonly listening: boolean;
  readonly pending: boolean;
  readonly indicator: IndusWakeIndicatorState;
  readonly phrase: string;
  /** Why it is not listening, as a message key with its values. */
  readonly notice: { key: MessageKey; vars?: Record<string, string> } | null;
}

interface StartAnswer {
  started?: boolean;
  reason?: string | null;
  hint?: string | null;
  phrase?: string;
}

const REASON_KEYS: Record<string, MessageKey> = {
  disabled: "wake.reason.disabled",
  consent_required: "wake.reason.consent",
  owned: "wake.reason.owned",
  unavailable: "wake.reason.unavailable",
};

class StartTimeout extends Error {}

export class WakeController {
  private view: WakeView = {
    enabled: false,
    listening: false,
    pending: false,
    indicator: "hidden",
    phrase: DEFAULT_PHRASE,
    notice: null,
  };
  private readonly listeners = new Set<() => void>();
  private readonly rpc: LooseRpc;
  private readonly timers: WakeTimers;
  private readonly setIndicator: (state: IndusWakeIndicatorState) => void;
  private readonly handOff: () => void;
  private serial = 0;

  constructor(options: {
    rpc: LooseRpc;
    /** Start voice capture for a detection (P24-35's dictation / conversation). */
    handOff: () => void;
    setIndicator?: (state: IndusWakeIndicatorState) => void;
    timers?: WakeTimers;
  }) {
    this.rpc = options.rpc;
    this.handOff = options.handOff;
    this.setIndicator =
      options.setIndicator ?? ((state) => desktopBridge()?.wake.setIndicator({ state }));
    this.timers = options.timers ?? {
      setTimeout: (fn, ms) => setTimeout(fn, ms),
      clearTimeout: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
    };
  }

  get(): WakeView {
    return this.view;
  }

  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  private patch(next: Partial<WakeView>): void {
    const before = this.view.indicator;
    this.view = { ...this.view, ...next };
    if (this.view.indicator !== before) this.setIndicator(this.view.indicator);
    for (const listener of [...this.listeners]) listener();
  }

  private withDeadline<T>(call: Promise<T>): Promise<T> {
    return new Promise<T>((resolve, reject) => {
      const timer = this.timers.setTimeout(() => reject(new StartTimeout()), WAKE_START_TIMEOUT_MS);
      call.then(
        (value) => {
          this.timers.clearTimeout(timer);
          resolve(value);
        },
        (error: unknown) => {
          this.timers.clearTimeout(timer);
          reject(error);
        },
      );
    });
  }

  /** Arm the listener. `consent` only from the explicit Settings switch (or `/wake on`). */
  async start(options: { consent: boolean }): Promise<void> {
    const serial = ++this.serial;
    this.patch({ pending: true, notice: null });
    let answer: StartAnswer;
    try {
      answer = (await this.withDeadline(
        this.rpc.request(
          "wake.start",
          { surface: "gui", consent: options.consent },
          { timeoutMs: WAKE_START_TIMEOUT_MS },
        ),
      )) as StartAnswer;
    } catch (error) {
      if (serial !== this.serial) return;
      // A launch arm (no consent) that finds no connection is not a failure the person asked
      // about: it stays quiet, and the switch simply reads off (W54-D2-3).
      const quiet = !options.consent && error instanceof Error && error.name === "NotConnected";
      this.patch({
        pending: false,
        listening: false,
        indicator: "hidden",
        notice: quiet
          ? null
          : error instanceof StartTimeout
            ? { key: "wake.reason.timeout" }
            : {
                key: "wake.reason.failed",
                vars: { message: error instanceof Error ? error.message : String(error) },
              },
      });
      return;
    }
    if (serial !== this.serial) return;
    const phrase = answer.phrase?.trim() || this.view.phrase;
    if (answer.started === true) {
      this.patch({
        pending: false,
        enabled: true,
        listening: true,
        indicator: "listening",
        phrase,
        notice: null,
      });
      return;
    }
    const key = REASON_KEYS[answer.reason ?? ""] ?? "wake.reason.disabled";
    // At launch (no consent) "it is off" is the normal answer, not news: the switch reading off
    // says it (W54-D2-3). The person's own switch still gets every reason.
    const routine =
      !options.consent && (key === "wake.reason.disabled" || key === "wake.reason.consent");
    this.patch({
      pending: false,
      listening: false,
      indicator: "hidden",
      phrase,
      notice: routine ? null : { key, vars: { hint: answer.hint ?? "" } },
    });
  }

  /** Disarm; `persist` (the Settings switch) also turns the feature off. */
  async stop(options: { persist: boolean }): Promise<void> {
    ++this.serial;
    this.patch({ listening: false, pending: false, indicator: "hidden", notice: null });
    if (options.persist) this.patch({ enabled: false });
    try {
      await this.rpc.request("wake.stop", { persist: options.persist });
    } catch {
      // the daemon went away: it released the microphone with the connection
    }
  }

  /** The daemon heard the phrase (`wake.detected`). */
  detected(): void {
    if (!this.view.listening) return;
    this.patch({ listening: false, indicator: "heard" });
    try {
      this.handOff();
      this.patch({ indicator: "capturing" });
    } catch {
      void this.captureDone();
    }
  }

  /** Voice capture finished: listen again. */
  async captureDone(): Promise<void> {
    if (this.view.indicator !== "capturing" && this.view.indicator !== "heard") return;
    try {
      const answer = (await this.rpc.request("wake.resume", {})) as { ok?: boolean };
      if (answer.ok === true) {
        this.patch({ listening: true, indicator: "listening" });
        return;
      }
    } catch {
      // fall through: not listening
    }
    this.patch({ listening: false, indicator: "hidden" });
  }

  /** Follow the connection's events (`wake.detected`). */
  attach(onEvent: LooseRpc["onEvent"]): () => void {
    return (
      onEvent?.((event) => {
        if (event.type === "wake.detected") this.detected();
      }) ?? (() => {})
    );
  }
}

export function useWake(controller: WakeController): WakeView {
  return useSyncExternalStore(
    (listener) => controller.subscribe(listener),
    () => controller.get(),
    () => controller.get(),
  );
}

const LIGHT: Record<Exclude<IndusWakeIndicatorState, "hidden">, string> = {
  listening: "bg-accent",
  heard: "bg-ok",
  capturing: "bg-danger",
};

export function WakeIndicator(props: { phrase?: string }) {
  const t = useT();
  const bridge = desktopBridge();
  const [state, setState] = useState<IndusWakeIndicatorState>("hidden");

  useEffect(() => {
    document.documentElement.dataset.window = "wake";
    document.body.style.background = "transparent";
    if (bridge === null) return;
    let live = true;
    let heard = false;
    const off = bridge.wake.onIndicator((next) => {
      heard = true;
      if (live) setState(next.state);
    });
    void bridge.wake
      .getIndicator()
      .then((next) => {
        if (live && !heard) setState(next.state);
      })
      .catch(() => {});
    return () => {
      live = false;
      off();
    };
  }, [bridge]);

  if (state === "hidden") return null;
  const label =
    state === "listening"
      ? t("wake.indicator.listening", { phrase: props.phrase ?? DEFAULT_PHRASE })
      : t(state === "heard" ? "wake.indicator.heard" : "wake.indicator.capturing");
  return (
    <main
      className="flex h-full w-full items-center justify-center"
      data-state={state}
      data-testid="wake-indicator"
    >
      <p
        role="status"
        className="flex items-center gap-2 rounded-full border border-line bg-raised px-3 py-1 text-xs text-fg shadow"
      >
        <span aria-hidden="true" className={`inline-block h-2 w-2 rounded-full ${LIGHT[state]}`} />
        {label}
      </p>
    </main>
  );
}

export function WakeToggle(props: { controller: WakeController }) {
  const t = useT();
  const view = useWake(props.controller);
  const on = view.listening || view.pending;
  return (
    <div className="flex flex-col gap-1" data-testid="wake-toggle">
      <label className="flex items-center gap-2 text-sm">
        <input
          type="checkbox"
          role="switch"
          aria-checked={on}
          checked={on}
          disabled={view.pending}
          onChange={(event) => {
            if (event.target.checked) void props.controller.start({ consent: true });
            else void props.controller.stop({ persist: true });
          }}
        />
        {t("wake.toggle.label")}
      </label>
      <p className="text-xs text-muted">{t("wake.toggle.hint", { phrase: view.phrase })}</p>
      {view.pending ? <p className="text-xs text-muted">{t("wake.toggle.starting")}</p> : null}
      {view.notice !== null ? (
        <p role="alert" className="text-xs text-warn">
          {t(view.notice.key, view.notice.vars)}
        </p>
      ) : null}
    </div>
  );
}
