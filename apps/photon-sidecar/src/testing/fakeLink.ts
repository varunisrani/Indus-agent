// A scriptable Spectrum link for the sidecar's tests (no SDK, no network).
//
// `FakePhoton.provider` hands out one `FakeLink` per open; tests drive the link state, deliver
// inbound messages and read back every outbound call. `createSpectrumLink` is a fake provider module
// (INDUS_PHOTON_PROVIDER) for tests that start the sidecar as a real process: once open it delivers
// one DM with a text attachment and answers every send with OUT<n>.

import type {
  LinkHandlers,
  LinkOptions,
  LinkProvider,
  LinkState,
  OutboundFile,
  RawContent,
  RawMessage,
  SpectrumLink,
} from "../link.ts";
import { LinkError } from "../link.ts";

export const ANA = "+15550000200";
export const ANA_SPACE = `any;-;${ANA}`;

export type Call =
  | { op: "text"; space: string; text: string; format: string }
  | { op: "link"; space: string; url: string }
  | { op: "file"; space: string; file: OutboundFile }
  | { op: "typing"; space: string; on: boolean }
  | { op: "react"; space: string; id: string; emoji: string }
  | { op: "unreact"; space: string; id: string }
  | { op: "read"; space: string; id: string };

export function rawMessage(overrides: Partial<RawMessage> = {}): RawMessage {
  return {
    id: "IN1",
    space: { id: ANA_SPACE, type: "dm" },
    sender: { id: ANA, name: "Ana" },
    fromUs: false,
    at: 1_767_258_000_000,
    content: { type: "text", text: "hello" },
    replyTo: null,
    ...overrides,
  };
}

export function textFile(
  name: string,
  text: string,
  extra: { mime?: string; voice?: boolean; size?: number | null } = {},
): RawContent {
  const data = new TextEncoder().encode(text);
  return {
    type: "file",
    name,
    mime: extra.mime ?? "text/plain",
    size: extra.size === undefined ? data.byteLength : extra.size,
    voice: extra.voice ?? false,
    read: async () => data,
  };
}

export class FakeLink implements SpectrumLink {
  closed = false;
  private readonly owner: FakePhoton;

  constructor(owner: FakePhoton) {
    this.owner = owner;
  }

  private async outbound(call: Call): Promise<string> {
    this.owner.calls.push(call);
    const failure = this.owner.failures.shift();
    if (failure !== undefined) throw failure;
    this.owner.nextId += 1;
    return `OUT${this.owner.nextId}`;
  }

  private async plain(call: Call): Promise<void> {
    this.owner.calls.push(call);
    const failure = this.owner.failures.shift();
    if (failure !== undefined) throw failure;
  }

  sendText(space: string, text: string, format: "markdown" | "plain"): Promise<string> {
    return this.outbound({ op: "text", space, text, format });
  }

  sendLink(space: string, url: string): Promise<string> {
    return this.outbound({ op: "link", space, url });
  }

  sendFile(space: string, file: OutboundFile): Promise<string> {
    return this.outbound({ op: "file", space, file });
  }

  typing(space: string, on: boolean): Promise<void> {
    return this.plain({ op: "typing", space, on });
  }

  react(space: string, id: string, emoji: string): Promise<void> {
    return this.plain({ op: "react", space, id, emoji });
  }

  unreact(space: string, id: string): Promise<void> {
    return this.plain({ op: "unreact", space, id });
  }

  markRead(space: string, id: string): Promise<void> {
    return this.plain({ op: "read", space, id });
  }

  async close(): Promise<void> {
    this.closed = true;
    this.owner.closes += 1;
  }
}

export class FakePhoton {
  readonly calls: Call[] = [];
  readonly failures: LinkError[] = [];
  readonly opened: LinkOptions[] = [];
  nextId = 0;
  closes = 0;
  /** Thrown by the next opens, one each (a refused or failing connection). */
  refusals: Error[] = [];
  /** The state reported once the link is open. */
  openState: LinkState = "open";
  link: FakeLink | null = null;
  private handlers: LinkHandlers | null = null;

  readonly provider: LinkProvider = async (options) => {
    this.opened.push(options);
    const refusal = this.refusals.shift();
    if (refusal !== undefined) throw refusal;
    this.handlers = options.handlers;
    this.link = new FakeLink(this);
    options.handlers.onState(this.openState);
    return this.link;
  };

  state(state: LinkState, detail?: string): void {
    this.handlers?.onState(state, detail);
  }

  deliver(overrides: Partial<RawMessage> = {}): void {
    this.handlers?.onMessage(rawMessage(overrides));
  }
}

/** The fake as a provider module: one inbound DM with a text attachment shortly after opening. */
export const createSpectrumLink: LinkProvider = async (options) => {
  const owner = new FakePhoton();
  const link = await owner.provider(options);
  setTimeout(() => {
    owner.deliver({
      id: "HELLO",
      content: {
        type: "group",
        items: [
          textFile("notes.txt", "fake notes\n"),
          { type: "text", text: "hello from the fake" },
        ],
      },
    });
  }, 50);
  return link;
};

export { LinkError };
