// A scriptable WhatsApp Web socket for the bridge's tests (no library, no network).
//
// `FakeWa.provider` hands out one `FakeSocket` per connect; tests open and close the connection,
// deliver inbound messages and read back every outbound call. `createWaSocket` is the same fake as
// a provider module (INDUS_WA_PROVIDER) for tests that start the bridge as a real process.

import type {
  MessageRef,
  OutboundMedia,
  ProviderOptions,
  RawMessage,
  SelfIds,
  SocketHandlers,
  WaProvider,
  WaSocket,
} from "../provider.ts";
import { WaError } from "../provider.ts";

export const OWN: SelfIds = { jid: "15550000999@s.whatsapp.net", lid: "88800000999@lid" };

export type Call =
  | { op: "text"; chat: string; text: string; quoted?: MessageRef }
  | { op: "media"; chat: string; media: OutboundMedia; quoted?: MessageRef }
  | { op: "presence"; chat: string; state: string }
  | { op: "react"; chat: string; target: MessageRef; emoji: string }
  | { op: "read"; chat: string; target: MessageRef }
  | { op: "pair"; phone: string };

export function rawMessage(overrides: Partial<RawMessage> = {}): RawMessage {
  return {
    id: "IN1",
    chat: "15550000200@s.whatsapp.net",
    fromMe: false,
    participant: null,
    senderAlt: null,
    pushName: "Ana",
    text: "hello",
    timestamp: 1_767_258_000,
    mentions: [],
    quoted: null,
    media: null,
    editOf: null,
    ...overrides,
  };
}

export class FakeSocket implements WaSocket {
  readonly calls: Call[];
  closed = false;
  private readonly owner: FakeWa;

  constructor(owner: FakeWa) {
    this.owner = owner;
    this.calls = owner.calls;
  }

  private async outbound(): Promise<string> {
    const failure = this.owner.failures.shift();
    if (failure !== undefined) throw failure;
    if (this.owner.hold !== null) await this.owner.hold;
    this.owner.nextId += 1;
    return `OUT${this.owner.nextId}`;
  }

  async sendText(chat: string, text: string, quoted?: MessageRef): Promise<string> {
    this.calls.push({ op: "text", chat, text, ...(quoted ? { quoted } : {}) });
    return this.outbound();
  }

  async sendMedia(chat: string, media: OutboundMedia, quoted?: MessageRef): Promise<string> {
    this.calls.push({ op: "media", chat, media, ...(quoted ? { quoted } : {}) });
    return this.outbound();
  }

  async presence(chat: string, state: "composing" | "paused"): Promise<void> {
    this.calls.push({ op: "presence", chat, state });
  }

  async react(chat: string, target: MessageRef, emoji: string): Promise<void> {
    this.calls.push({ op: "react", chat, target, emoji });
  }

  async markRead(chat: string, target: MessageRef): Promise<void> {
    this.calls.push({ op: "read", chat, target });
  }

  async requestPairingCode(phone: string): Promise<string> {
    this.calls.push({ op: "pair", phone });
    return "ABCD1234";
  }

  async close(): Promise<void> {
    this.closed = true;
  }
}

export class FakeWa {
  readonly calls: Call[] = [];
  readonly sockets: FakeSocket[] = [];
  readonly failures: WaError[] = [];
  handlers: SocketHandlers | null = null;
  options: ProviderOptions | null = null;
  /** Open the connection as soon as a socket is made. */
  autoOpen = true;
  /** Fail this many connects before one succeeds. */
  refuseConnects = 0;
  self: SelfIds = OWN;
  nextId = 0;
  hold: Promise<void> | null = null;

  readonly provider: WaProvider = async (options) => {
    this.options = options;
    if (this.refuseConnects > 0) {
      this.refuseConnects -= 1;
      throw new WaError("not_connected", "refused");
    }
    this.handlers = options.handlers;
    const socket = new FakeSocket(this);
    this.sockets.push(socket);
    if (this.autoOpen) queueMicrotask(() => this.open());
    return socket;
  };

  open(): void {
    this.handlers?.onUpdate({ state: "open", self: this.self });
  }

  qr(code = "2@fake-qr"): void {
    this.handlers?.onUpdate({ state: "qr", qr: code });
  }

  close(loggedOut = false): void {
    this.handlers?.onUpdate({
      state: "closed",
      loggedOut,
      reason: loggedOut ? "status 401" : "status 428",
    });
  }

  deliver(...messages: Array<Partial<RawMessage>>): void {
    this.handlers?.onMessages(messages.map((m) => rawMessage(m)));
  }

  fail(code: WaError["code"], retryAfter: number | null = null): void {
    this.failures.push(new WaError(code, code, retryAfter));
  }

  /** Make outbound calls wait until the returned release is called. */
  holdSends(): () => void {
    let release: () => void = () => {};
    this.hold = new Promise<void>((resolve) => {
      release = () => {
        this.hold = null;
        resolve();
      };
    });
    return release;
  }
}

/** The fake as a provider module: opens at once as `OWN` and delivers one greeting from Ana
 * (with a small text document) right after, so a test driving the real process sees inbound
 * traffic too. */
export const createWaSocket: WaProvider = async (options) => {
  const wa = new FakeWa();
  const socket = await wa.provider(options);
  setTimeout(() => {
    wa.deliver({
      id: "HELLO",
      text: "hello from the fake",
      media: {
        kind: "document",
        mime: "text/plain",
        name: "notes.txt",
        size: 11,
        download: async () => new TextEncoder().encode("fake notes\n"),
      },
    });
  }, 50);
  return socket;
};
