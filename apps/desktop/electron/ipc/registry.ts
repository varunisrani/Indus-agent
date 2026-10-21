// The only place that talks to `ipcMain` (P20-03, D-16.18). Every handler goes through
// `registerIpc(channel, schema, handler)` — usually through the typed `handle` / `on` / `sync`
// helpers, which take the schema from `schemas.ts`. Before a handler runs:
//
//   1. the sender is checked (`checkSender`: main frame, app origin, known window);
//   2. the payload is parsed by the channel's schema (strict objects: unknown keys are refused).
//
// A refusal answers a fixed error envelope and is logged once per channel and reason; the handler
// never sees the message. `scripts/check-ipc.mjs` fails CI when any other file under `electron/`
// touches `ipcMain` or `webContents.ipc`.

import type { z } from "zod";
import {
  CHANNEL_PREFIX,
  CHANNELS,
  type ChannelName,
  channelDef,
  type EventChannel,
  type InvokeChannel,
  type SendChannel,
  type SyncChannel,
} from "./channels.ts";
import {
  type Envelope,
  errorEnvelope,
  IpcError,
  okEnvelope,
  SENDER_REJECTED_MESSAGE,
} from "./envelope.ts";
import { type PayloadOf, type RequestOf, type ResponseOf, SCHEMAS } from "./schemas.ts";
import { checkSender, type IpcEventLike, type SenderPolicy } from "./senderCheck.ts";

export interface SyncEventLike extends IpcEventLike {
  returnValue: unknown;
}

/** The parts of Electron's `ipcMain` the registry uses. */
export interface IpcMainLike {
  handle(channel: string, listener: (event: IpcEventLike, ...args: unknown[]) => unknown): void;
  on(channel: string, listener: (event: SyncEventLike, ...args: unknown[]) => void): unknown;
}

export interface HandlerContext {
  /** `webContents.id` of the (checked) sender. */
  readonly senderId: number;
  readonly event: IpcEventLike;
}

export interface RegistryOptions {
  ipc: IpcMainLike;
  policy: SenderPolicy;
  log: (line: string) => void;
}

export interface EventTarget {
  send(channel: string, payload: unknown): void;
  isDestroyed?(): boolean;
}

export class IpcRegistrationError extends Error {}

type AnyHandler = (payload: unknown, context: HandlerContext) => unknown;

export class IpcRegistry {
  private readonly options: RegistryOptions;
  private readonly names = new Set<string>();
  private readonly loggedOnce = new Set<string>();
  private sealed = false;

  constructor(options: RegistryOptions) {
    this.options = options;
  }

  /** Channel names with a handler (pending `not-available` ones included after `seal`). */
  registered(): string[] {
    return [...this.names];
  }

  /**
   * Register `handler` for `channel` (an `invoke` or `send` channel from the table). `schema`
   * parses the payload; a sender or payload refusal never reaches the handler.
   */
  registerIpc(channel: string, schema: z.ZodType, handler: AnyHandler): void {
    const def = this.claim(channel);
    if (def.kind === "invoke") {
      this.options.ipc.handle(channel, async (event, ...args) => {
        const refused = this.refuse(channel, event);
        if (refused !== null) return refused;
        const parsed = this.parse(channel, schema, args);
        if (!parsed.ok) return parsed.envelope;
        try {
          return okEnvelope(await handler(parsed.value, { senderId: event.sender.id, event }));
        } catch (error) {
          return this.failure(channel, error);
        }
      });
    } else if (def.kind === "send") {
      this.options.ipc.on(channel, (event, ...args) => {
        if (this.refuse(channel, event) !== null) return;
        const parsed = this.parse(channel, schema, args);
        if (!parsed.ok) return;
        try {
          const result = handler(parsed.value, { senderId: event.sender.id, event });
          if (result instanceof Promise)
            result.catch((error: unknown) => this.failure(channel, error));
        } catch (error) {
          this.failure(channel, error);
        }
      });
    } else {
      throw new IpcRegistrationError(`${channel} is a ${def.kind} channel; use sync() or emit()`);
    }
  }

  /** Typed `invoke` handler; the payload is already checked and parsed. */
  handle<N extends InvokeChannel>(
    name: N,
    handler: (
      payload: RequestOf<N>,
      context: HandlerContext,
    ) => ResponseOf<N> | Promise<ResponseOf<N>>,
  ): void {
    const schemas = SCHEMAS[name] as { request: z.ZodType };
    this.registerIpc(name, schemas.request, handler as AnyHandler);
  }

  /** Typed `send` handler (no answer). */
  on<N extends SendChannel>(
    name: N,
    handler: (payload: RequestOf<N>, context: HandlerContext) => void,
  ): void {
    const schemas = SCHEMAS[name] as { request: z.ZodType };
    this.registerIpc(name, schemas.request, handler as AnyHandler);
  }

  /** Typed `sync` handler (launch facts): answers through `event.returnValue`. */
  sync<N extends SyncChannel>(name: N, handler: (context: HandlerContext) => ResponseOf<N>): void {
    this.claim(name, "sync");
    this.options.ipc.on(name, (event) => {
      const refused = this.refuse(name, event);
      if (refused !== null) {
        event.returnValue = refused;
        return;
      }
      try {
        event.returnValue = okEnvelope(handler({ senderId: event.sender.id, event }));
      } catch (error) {
        event.returnValue = this.failure(name, error);
      }
    });
  }

  /** Push `payload` on event channel `name` to one window; a payload off its schema is dropped. */
  emit<N extends EventChannel>(target: EventTarget, name: N, payload: PayloadOf<N>): boolean {
    const schema = (SCHEMAS[name] as { payload: z.ZodType }).payload;
    const checked = schema.safeParse(payload);
    if (!checked.success) {
      this.once(`${name}:bad-event`, `[ipc] ${name}: event payload off its schema; dropped`);
      return false;
    }
    if (target.isDestroyed?.() === true) return false;
    try {
      target.send(name, checked.data);
      return true;
    } catch {
      return false;
    }
  }

  /**
   * After every install step: each declared invoke/sync channel still without a handler answers
   * `not-available`, so a renderer slice can run before the main slice that owns the channel.
   */
  seal(): void {
    if (this.sealed) return;
    this.sealed = true;
    for (const def of CHANNELS) {
      if (this.names.has(def.name)) continue;
      if (def.kind === "invoke") {
        this.names.add(def.name);
        this.options.ipc.handle(def.name, (event) => {
          const refused = this.refuse(def.name, event);
          if (refused !== null) return refused;
          return errorEnvelope("not-available", `${def.name} is not available in this build yet`);
        });
      } else if (def.kind === "sync") {
        this.names.add(def.name);
        this.options.ipc.on(def.name, (event) => {
          event.returnValue = errorEnvelope("not-available", `${def.name} is not available`);
        });
      }
    }
  }

  private claim(channel: string, kind?: "sync") {
    if (!channel.startsWith(CHANNEL_PREFIX)) {
      throw new IpcRegistrationError(
        `IPC channel ${JSON.stringify(channel)} must start with ${CHANNEL_PREFIX}`,
      );
    }
    const def = channelDef(channel);
    if (def === undefined) throw new IpcRegistrationError(`${channel} is not in the channel table`);
    if (kind !== undefined && def.kind !== kind) {
      throw new IpcRegistrationError(`${channel} is a ${def.kind} channel, not ${kind}`);
    }
    if (this.sealed) throw new IpcRegistrationError(`${channel} registered after seal()`);
    if (this.names.has(channel)) throw new IpcRegistrationError(`${channel} already has a handler`);
    this.names.add(channel);
    return def;
  }

  private refuse(channel: string, event: IpcEventLike): Envelope<never> | null {
    const reason = checkSender(event, this.options.policy);
    if (reason === null) return null;
    this.once(`${channel}:${reason}`, `[ipc] ${channel}: sender refused (${reason})`);
    return errorEnvelope("sender-rejected", SENDER_REJECTED_MESSAGE);
  }

  private parse(
    channel: string,
    schema: z.ZodType,
    args: unknown[],
  ): { ok: true; value: unknown } | { ok: false; envelope: Envelope<never> } {
    if (args.length > 1) {
      this.once(`${channel}:arity`, `[ipc] ${channel}: more than one argument; refused`);
      return { ok: false, envelope: errorEnvelope("bad-request", "one payload argument expected") };
    }
    const result = schema.safeParse(args[0]);
    if (result.success) return { ok: true, value: result.data };
    this.once(`${channel}:bad-request`, `[ipc] ${channel}: malformed payload; refused`);
    const first = result.error.issues[0];
    const where = first === undefined || first.path.length === 0 ? "payload" : first.path.join(".");
    return { ok: false, envelope: errorEnvelope("bad-request", `invalid ${where}`) };
  }

  private failure(channel: string, error: unknown): Envelope<never> {
    if (error instanceof IpcError) return errorEnvelope(error.code, error.message);
    const name = error instanceof Error ? error.name : typeof error;
    this.options.log(`[ipc] ${channel}: handler failed (${name})`);
    return errorEnvelope("internal", "the desktop app could not complete this request");
  }

  private once(key: string, line: string): void {
    if (this.loggedOnce.has(key)) return;
    this.loggedOnce.add(key);
    this.options.log(line);
  }
}

/** Every channel name in the table (for checks and tests). */
export function allChannelNames(): ChannelName[] {
  return CHANNELS.map((c) => c.name);
}
