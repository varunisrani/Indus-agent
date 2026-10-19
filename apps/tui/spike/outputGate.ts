// Spike (P12-01, throwaway): our writer around Ink's stdout (the D-04.2 fallback shape).
// Two jobs: drop Ink's DEC 2026 frame markers where they must not go (tmux, unknown terminals)
// and coalesce resize bursts before Ink sees them (leading + trailing, 32 ms).

import { seq } from "./terminalModes.ts";

export interface GateStats {
  frames: number;
  markersDropped: number;
  bytes: number;
  resizeEventsIn: number;
  resizeCallsOut: number;
}

export interface GateOptions {
  syncAllowed: boolean;
  resizeIntervalMs?: number;
  coalesceResize?: boolean;
}

type Listener = (...args: unknown[]) => void;
type Stdout = NodeJS.WriteStream;

/** Leading + trailing throttle: run at once, then at most once per interval, always a last one. */
export function coalesce(run: () => void, intervalMs: number): { (): void; cancel(): void } {
  let timer: ReturnType<typeof setTimeout> | undefined;
  let pending = false;
  const fire = (): void => {
    timer = undefined;
    if (!pending) return;
    pending = false;
    run();
    timer = setTimeout(fire, intervalMs);
  };
  const trigger = (): void => {
    if (timer === undefined) {
      run();
      timer = setTimeout(fire, intervalMs);
      return;
    }
    pending = true;
  };
  trigger.cancel = (): void => {
    if (timer !== undefined) clearTimeout(timer);
    timer = undefined;
    pending = false;
  };
  return trigger;
}

export function gateStdout(
  stdout: Stdout,
  options: GateOptions,
): { stream: Stdout; stats: GateStats } {
  const stats: GateStats = {
    frames: 0,
    markersDropped: 0,
    bytes: 0,
    resizeEventsIn: 0,
    resizeCallsOut: 0,
  };
  const interval = options.resizeIntervalMs ?? 32;
  const wrapped = new Map<Listener, Listener & { cancel?: () => void }>();

  const write = (chunk: unknown, ...rest: unknown[]): boolean => {
    const text = typeof chunk === "string" ? chunk : String(chunk);
    if (text === seq.syncBegin) stats.frames += 1;
    if ((text === seq.syncBegin || text === seq.syncEnd) && !options.syncAllowed) {
      stats.markersDropped += 1;
      const callback = rest.find((value) => typeof value === "function") as Listener | undefined;
      callback?.();
      return true;
    }
    stats.bytes += Buffer.byteLength(text);
    return (stdout.write as (...a: unknown[]) => boolean)(chunk, ...rest);
  };

  const on = (event: string, listener: Listener): Stdout => {
    if (event !== "resize" || options.coalesceResize === false) {
      stdout.on(event, listener);
      return proxy;
    }
    const throttled = coalesce(() => {
      stats.resizeCallsOut += 1;
      listener();
    }, interval);
    const counted = Object.assign(
      () => {
        stats.resizeEventsIn += 1;
        throttled();
      },
      { cancel: throttled.cancel },
    );
    wrapped.set(listener, counted);
    stdout.on(event, counted);
    return proxy;
  };

  const off = (event: string, listener: Listener): Stdout => {
    const inner = wrapped.get(listener);
    if (inner) {
      inner.cancel?.();
      wrapped.delete(listener);
      stdout.off(event, inner);
    } else {
      stdout.off(event, listener);
    }
    return proxy;
  };

  const proxy: Stdout = new Proxy(stdout, {
    get(target, key) {
      if (key === "write") return write;
      if (key === "on" || key === "addListener") return on;
      if (key === "off" || key === "removeListener") return off;
      const value = Reflect.get(target, key, target) as unknown;
      return typeof value === "function" ? (value as Listener).bind(target) : value;
    },
  });
  return { stream: proxy, stats };
}
