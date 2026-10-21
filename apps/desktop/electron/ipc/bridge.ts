// Builds the `window.indusDesktop` object from the channel table (P20-05). Pure: the preload
// passes Electron's `ipcRenderer`, tests pass a fake. Every function here is a thin wrapper over
// one channel; none returns or captures anything but payloads — no `ipcRenderer`, no event
// object, no sender, no port.

import { CHANNELS, type ChannelDef } from "./channels.ts";
import { unwrapEnvelope } from "./envelope.ts";

/** The parts of Electron's `ipcRenderer` the bridge uses. */
export interface RendererIpc {
  invoke(channel: string, ...args: unknown[]): Promise<unknown>;
  send(channel: string, ...args: unknown[]): void;
  sendSync(channel: string, ...args: unknown[]): unknown;
  on(channel: string, listener: (event: unknown, ...args: unknown[]) => void): unknown;
  removeListener(channel: string, listener: (event: unknown, ...args: unknown[]) => void): unknown;
}

export type BridgeObject = Readonly<Record<string, unknown>>;

/** Facts a window falls back to when its sync read is refused (it still loads). */
export const FALLBACK_FACTS = Object.freeze({
  platform: "unknown",
  appVersion: "0.0.0",
  packaged: true,
  dev: false,
  windowKind: "main",
  watch: false,
  profile: null,
  homeDir: "",
  titleBarHeight: 34,
  deepLinkScheme: "indus",
});

/** Report a listener's error without breaking the other listeners (window.onerror sees it). */
function reportLater(error: unknown): void {
  queueMicrotask(() => {
    throw error;
  });
}

function invoker(ipc: RendererIpc, name: string) {
  return async (payload?: unknown): Promise<unknown> =>
    unwrapEnvelope(await ipc.invoke(name, payload));
}

function sender(ipc: RendererIpc, name: string) {
  return (payload?: unknown): void => {
    ipc.send(name, payload);
  };
}

function subscriber(ipc: RendererIpc, name: string) {
  return (listener: (payload: unknown) => void): (() => void) => {
    if (typeof listener !== "function") throw new TypeError("a listener function is required");
    const wrapped = (_event: unknown, payload: unknown) => {
      try {
        listener(payload);
      } catch (error) {
        reportLater(error);
      }
    };
    ipc.on(name, wrapped);
    let active = true;
    return () => {
      if (!active) return;
      active = false;
      ipc.removeListener(name, wrapped);
    };
  };
}

function readFacts(ipc: RendererIpc, name: string): unknown {
  try {
    const facts = unwrapEnvelope(ipc.sendSync(name));
    return facts !== null && typeof facts === "object"
      ? Object.freeze({ ...facts })
      : FALLBACK_FACTS;
  } catch {
    return FALLBACK_FACTS;
  }
}

/** The bridge object for `channels` (default: the whole table). */
export function buildBridge(
  ipc: RendererIpc,
  channels: readonly ChannelDef[] = CHANNELS,
): BridgeObject {
  const groups = new Map<string, Record<string, unknown>>();
  const values = new Map<string, unknown>();
  for (const def of channels) {
    if (def.kind === "sync") {
      values.set(def.group, readFacts(ipc, def.name));
      continue;
    }
    if (def.method === null) continue;
    const group = groups.get(def.group) ?? {};
    groups.set(def.group, group);
    group[def.method] =
      def.kind === "invoke"
        ? invoker(ipc, def.name)
        : def.kind === "send"
          ? sender(ipc, def.name)
          : subscriber(ipc, def.name);
  }
  const bridge: Record<string, unknown> = {};
  for (const [name, value] of values) bridge[name] = value;
  for (const [name, methods] of groups) bridge[name] = Object.freeze(methods);
  return Object.freeze(bridge);
}
