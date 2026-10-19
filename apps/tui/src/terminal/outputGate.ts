// Our writer around Ink's stdout (D-04.2): drops Ink's DEC 2026 frame markers where they must
// not go (R05.17), coalesces resize events before Ink sees them (R05.16), and reports write
// outcomes to the dead-stream guard.

import { seq } from "./modes.ts";
import { createResizeCoalescer, RESIZE_INTERVAL_MS, type ResizeCoalescer } from "./resize.ts";

export interface GateOptions {
  syncOutput: boolean;
  resizeIntervalMs?: number;
  onWriteError?: (error: unknown) => void;
  onWriteOk?: () => void;
}

export interface GateStats {
  frames: number;
  markersDropped: number;
  resizeIn: number;
}

type Listener = (...args: unknown[]) => void;
type Out = NodeJS.WriteStream;

export function gateStdout(stdout: Out, options: GateOptions): { stream: Out; stats: GateStats } {
  const stats: GateStats = { frames: 0, markersDropped: 0, resizeIn: 0 };
  const coalescers = new Map<Listener, { coalescer: ResizeCoalescer; listener: Listener }>();
  const interval = options.resizeIntervalMs ?? RESIZE_INTERVAL_MS;

  const write = (chunk: unknown, ...rest: unknown[]): boolean => {
    const text = typeof chunk === "string" ? chunk : String(chunk);
    if (text === seq.syncBegin) stats.frames += 1;
    if (!options.syncOutput && (text === seq.syncBegin || text === seq.syncEnd)) {
      stats.markersDropped += 1;
      const done = rest.find((value) => typeof value === "function") as Listener | undefined;
      done?.();
      return true;
    }
    let body = text;
    if (!options.syncOutput && (text.includes(seq.syncBegin) || text.includes(seq.syncEnd))) {
      body = text.replaceAll(seq.syncBegin, "").replaceAll(seq.syncEnd, "");
      stats.markersDropped += 1;
    }
    try {
      const ok = (stdout.write as (...args: unknown[]) => boolean)(body, ...rest);
      options.onWriteOk?.();
      return ok;
    } catch (error) {
      options.onWriteError?.(error);
      return false;
    }
  };

  const on = (event: string, listener: Listener): Out => {
    if (event !== "resize") {
      stdout.on(event, listener);
      return proxy;
    }
    const coalescer = createResizeCoalescer(() => listener(), interval);
    const counted: Listener = () => {
      stats.resizeIn += 1;
      coalescer.schedule();
    };
    coalescers.set(listener, { coalescer, listener: counted });
    stdout.on(event, counted);
    return proxy;
  };

  const off = (event: string, listener: Listener): Out => {
    const entry = coalescers.get(listener);
    if (event === "resize" && entry !== undefined) {
      entry.coalescer.cancel();
      coalescers.delete(listener);
      stdout.off(event, entry.listener);
    } else {
      stdout.off(event, listener);
    }
    return proxy;
  };

  const proxy: Out = new Proxy(stdout, {
    get(target, prop) {
      if (prop === "write") return write;
      if (prop === "on" || prop === "addListener") return on;
      if (prop === "off" || prop === "removeListener") return off;
      const value = Reflect.get(target, prop, target) as unknown;
      return typeof value === "function" ? (value as Listener).bind(target) : value;
    },
  });
  return { stream: proxy, stats };
}
