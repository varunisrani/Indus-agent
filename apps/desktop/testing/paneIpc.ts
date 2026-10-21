// Test harness for the panes' main-process channels (P20-28..31): a real `IpcRegistry` over a fake
// `ipcMain`, windows with known ids, and fake web contents that record what main pushes.

import { fileAppOrigin } from "../electron/appOrigin.ts";
import type { ChannelName } from "../electron/ipc/channels.ts";
import { unwrapEnvelope } from "../electron/ipc/envelope.ts";
import { type IpcMainLike, IpcRegistry } from "../electron/ipc/registry.ts";
import type { IpcEventLike } from "../electron/ipc/senderCheck.ts";

const APP_URL = "file:///opt/Indus/app/dist/index.html#/";
const ORIGIN = fileAppOrigin("/opt/Indus/app/dist/index.html", "linux");

type Listener = (event: IpcEventLike, ...args: unknown[]) => unknown;

/** A window's web contents: records events, can be destroyed. */
export class FakeContents {
  readonly id: number;
  readonly sent: Array<{ channel: string; payload: unknown }> = [];
  private destroyed = false;
  private readonly destroyedListeners: Array<() => void> = [];

  constructor(id: number) {
    this.id = id;
  }

  send(channel: string, payload: unknown): void {
    this.sent.push({ channel, payload });
  }

  isDestroyed(): boolean {
    return this.destroyed;
  }

  on(event: "destroyed", listener: () => void): this {
    if (event === "destroyed") this.destroyedListeners.push(listener);
    return this;
  }

  destroy(): void {
    this.destroyed = true;
    for (const listener of this.destroyedListeners.splice(0)) listener();
  }

  events(channel: string): unknown[] {
    return this.sent.filter((e) => e.channel === channel).map((e) => e.payload);
  }
}

export interface PaneIpcHarness {
  registry: IpcRegistry;
  log: string[];
  windows: Map<number, FakeContents>;
  window(id: number): FakeContents;
  contents: (id: number) => FakeContents | null;
  /** Invoke `channel` as window `senderId`; unwraps the envelope (throws `Error("code: …")`). */
  invoke(channel: ChannelName, payload: unknown, senderId?: number): Promise<unknown>;
  /** Send on a `send` channel as window `senderId`. */
  send(channel: ChannelName, payload: unknown, senderId?: number): void;
}

export function paneIpcHarness(windowIds: readonly number[] = [11, 12]): PaneIpcHarness {
  const handles = new Map<string, Listener>();
  const ons = new Map<string, Listener>();
  const ipc: IpcMainLike = {
    handle: (channel, listener) => {
      handles.set(channel, listener as Listener);
    },
    on: (channel, listener) => {
      ons.set(channel, listener as Listener);
      return ipc;
    },
  };
  const windows = new Map<number, FakeContents>();
  for (const id of windowIds) windows.set(id, new FakeContents(id));
  const log: string[] = [];
  const registry = new IpcRegistry({
    ipc,
    policy: { appOrigin: ORIGIN, isKnownWebContents: (id) => windows.has(id) },
    log: (line) => log.push(line),
  });
  const event = (senderId: number): IpcEventLike => {
    const frame = { url: APP_URL, processId: 4, routingId: 1, frameTreeNodeId: 1 };
    return {
      sender: { id: senderId, mainFrame: frame, getType: () => "window" },
      senderFrame: frame,
    };
  };
  const first = windowIds[0] ?? 11;
  return {
    registry,
    log,
    windows,
    window: (id) => {
      const found = windows.get(id);
      if (found === undefined) throw new Error(`no window ${id}`);
      return found;
    },
    contents: (id) => {
      const found = windows.get(id);
      return found === undefined || found.isDestroyed() ? null : found;
    },
    invoke: async (channel, payload, senderId = first) => {
      const handler = handles.get(channel);
      if (handler === undefined) throw new Error(`no handler for ${channel}`);
      return unwrapEnvelope(await handler(event(senderId), payload));
    },
    send: (channel, payload, senderId = first) => {
      const handler = ons.get(channel);
      if (handler === undefined) throw new Error(`no handler for ${channel}`);
      handler(event(senderId), payload);
    },
  };
}
