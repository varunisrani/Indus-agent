// Test doubles for the chat surfaces: a scripted connection (answers calls, records them, emits
// session events) and protocol errors built by name.

import { GatewayRpcError } from "@indus/client";
import { ERROR_CODES, type ErrorName } from "@indus/protocol";
import type { ChatConnection, ConnectionMode, SessionEvent } from "../gateway/connectionStore.ts";

export function rpcError(name: ErrorName, message: string = name): GatewayRpcError {
  return new GatewayRpcError(ERROR_CODES[name].code, message);
}

type Handler = (params: Record<string, unknown>) => unknown;

export class FakeConnection implements ChatConnection {
  readonly mode: ConnectionMode;
  readonly codeVersion: string;
  readonly calls: Array<{ method: string; params: Record<string, unknown> }> = [];
  readonly handlers = new Map<string, Handler>();
  reconnects = 0;
  private readonly listeners = new Map<string, Set<(event: SessionEvent) => void>>();
  private seq = 0;

  constructor(options: { mode?: ConnectionMode; codeVersion?: string } = {}) {
    this.mode = options.mode ?? "local";
    this.codeVersion = options.codeVersion ?? "1.2.3";
  }

  on(method: string, handler: Handler): this {
    this.handlers.set(method, handler);
    return this;
  }

  request = (async (method: string, params: unknown) => {
    const p = (params ?? {}) as Record<string, unknown>;
    this.calls.push({ method, params: p });
    const handler = this.handlers.get(method);
    if (handler === undefined) throw rpcError("METHOD_NOT_FOUND", `no fake for ${method}`);
    return handler(p);
  }) as ChatConnection["request"];

  onSessionEvent(sessionId: string, listener: (event: SessionEvent) => void): () => void {
    let set = this.listeners.get(sessionId);
    if (set === undefined) {
      set = new Set();
      this.listeners.set(sessionId, set);
    }
    set.add(listener);
    return () => set?.delete(listener);
  }

  emit(sessionId: string, event: Record<string, unknown>): void {
    this.seq += 1;
    const full = { session_id: sessionId, seq: this.seq, ...event } as SessionEvent;
    for (const listener of this.listeners.get(sessionId) ?? []) listener(full);
  }

  reconnect(): void {
    this.reconnects += 1;
  }

  called(method: string) {
    return this.calls.filter((call) => call.method === method);
  }
}

export class MemoryStorage {
  readonly data = new Map<string, string>();
  getItem(key: string): string | null {
    return this.data.get(key) ?? null;
  }
  setItem(key: string, value: string): void {
    this.data.set(key, value);
  }
  removeItem(key: string): void {
    this.data.delete(key);
  }
}
