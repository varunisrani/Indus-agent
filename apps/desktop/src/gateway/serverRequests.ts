// Open server requests of one connection (approval, clarify, sudo, secret), as cards the renderer
// shows. The channel hands each request to a handler that waits for the person; this store keeps
// those waits by id. First answer wins (R08.19): a second answer, a double click or a card the
// daemon re-offered after a reconnect never produces a second frame on the wire.
//
// The preview kinds (`preview.act` / `preview.read`, P24-25) are no cards: with a `preview`
// handler (the window's preview automation) they go straight to it; the window that shows the
// session's preview answers, any other one declines "not shown here" through the channel.

import {
  declineAnswer,
  type IncomingRequest,
  type RpcChannel,
  type ServerRequestHandlers,
} from "@indus/client";
import type {
  PreviewActRequest,
  PreviewReadRequest,
  ServerRequest,
  ServerRequestAnswer,
  ServerRequestKind,
} from "@indus/protocol";

export interface PendingServerRequest {
  /** The request's `request_id`. */
  readonly id: string;
  readonly kind: ServerRequestKind;
  readonly sessionId: string;
  /** The typed payload (`ApprovalRequest`, `ClarifyRequest`, ...). */
  readonly request: ServerRequest;
  /** Re-offered after a reconnect (or a resume) rather than first asked on this link. */
  readonly replayed: boolean;
  readonly receivedAt: number;
}

interface Waiting {
  readonly card: PendingServerRequest;
  readonly settle: (answer: ServerRequestAnswer) => void;
}

/** Ids remembered after their card closed, so a late repeat is still recognised. */
const SETTLED_MEMORY = 512;

const KINDS: readonly ServerRequestKind[] = ["approval", "clarify", "sudo", "secret"];

/** The preview request kinds a window answers for the session preview it shows. */
export const PREVIEW_KINDS = ["preview.act", "preview.read"] as const;

type PreviewServerRequest = PreviewActRequest | PreviewReadRequest;

/** What the window's preview automation answers: the page's result, or "not shown here". */
export type PreviewHandlerAnswer =
  | { readonly declined: "not_shown" }
  | ({ readonly ok: boolean } & Readonly<Record<string, unknown>>);

export interface PreviewHandler {
  handle(request: PreviewServerRequest): Promise<PreviewHandlerAnswer>;
}

export interface ServerRequestStoreOptions {
  now?: () => number;
  /** The channel whose `decline` sends the "not shown here" refusal. */
  channel?: () => Pick<RpcChannel, "decline"> | null;
  /** Answers the preview kinds (absent: they are refused like any kind nothing here handles). */
  preview?: PreviewHandler;
}

/** The protocol answer for what the page reported (`ok`, `error`, the rest as `result`). */
export function previewAnswer(
  kind: PreviewServerRequest["kind"],
  reported: { readonly ok: boolean } & Readonly<Record<string, unknown>>,
): ServerRequestAnswer {
  const { ok, error, ...rest } = reported;
  const result = JSON.parse(JSON.stringify(rest)) as Record<string, never>;
  return { kind, ok, error: typeof error === "string" ? error : null, result };
}

export class ServerRequestStore {
  private readonly waiting = new Map<string, Waiting>();
  private readonly settled = new Set<string>();
  private readonly listeners = new Set<() => void>();
  private readonly options: ServerRequestStoreOptions;
  private snapshot: readonly PendingServerRequest[] = [];
  private readonly bySession = new Map<string, readonly PendingServerRequest[]>();

  constructor(options: ServerRequestStoreOptions = {}) {
    this.options = options;
  }

  /** Open requests in arrival order, all or one session's. The array is stable between changes. */
  list(sessionId?: string): readonly PendingServerRequest[] {
    if (sessionId === undefined) return this.snapshot;
    let cached = this.bySession.get(sessionId);
    if (cached === undefined) {
      cached = this.snapshot.filter((card) => card.sessionId === sessionId);
      this.bySession.set(sessionId, cached);
    }
    return cached;
  }

  get(id: string): PendingServerRequest | undefined {
    return this.waiting.get(id)?.card;
  }

  isAnswered(id: string): boolean {
    return this.settled.has(id);
  }

  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  /** Answer an open request. False when it was already answered, withdrawn or never seen. */
  answer(id: string, answer: ServerRequestAnswer): boolean {
    const entry = this.waiting.get(id);
    if (entry === undefined || this.settled.has(id)) return false;
    if (answer.kind !== entry.card.kind) return false;
    this.close(id);
    entry.settle(answer);
    return true;
  }

  /**
   * "Not shown here": the channel sends `server_request.decline` only to a daemon that counts
   * declines, and the plain refusal otherwise. False when already answered or unknown.
   */
  decline(id: string): boolean {
    const entry = this.waiting.get(id);
    if (entry === undefined || this.settled.has(id)) return false;
    this.close(id);
    // The channel finishes the id first (its decline frame or refusal); the wait then settles
    // with the kind's decline answer, which the channel drops as a second answer. Without a
    // channel that answer is the refusal itself.
    this.options.channel?.()?.decline(id);
    entry.settle(declineAnswer(entry.card.request));
    return true;
  }

  /** The handler table for `IndusClient.handle({ serverRequests })`. */
  handlers(): ServerRequestHandlers {
    const table: Record<string, (request: ServerRequest, meta: IncomingRequest) => unknown> = {};
    for (const kind of KINDS) {
      table[kind] = (request, meta) => this.accept(request, meta);
    }
    const preview = this.options.preview;
    if (preview !== undefined) {
      for (const kind of PREVIEW_KINDS) {
        table[kind] = (request) => this.drive(preview, request as PreviewServerRequest);
      }
    }
    return table as ServerRequestHandlers;
  }

  /** Drop every card (the connection is gone for good). Waits are left to the channel. */
  clear(): void {
    if (this.waiting.size === 0) return;
    for (const id of [...this.waiting.keys()]) this.close(id);
  }

  private async drive(
    preview: PreviewHandler,
    request: PreviewServerRequest,
  ): Promise<ServerRequestAnswer> {
    let reported: PreviewHandlerAnswer;
    try {
      reported = await preview.handle(request);
    } catch (error) {
      reported = { ok: false, error: error instanceof Error ? error.message : String(error) };
    }
    if ("declined" in reported) {
      // The channel sends the decline (or the plain refusal); this answer is then dropped.
      this.options.channel?.()?.decline(request.request_id);
      return declineAnswer(request);
    }
    return previewAnswer(request.kind, reported);
  }

  private accept(request: ServerRequest, meta: IncomingRequest): Promise<ServerRequestAnswer> {
    return new Promise<ServerRequestAnswer>((resolve) => {
      const id = request.request_id;
      if (this.waiting.has(id) || this.settled.has(id)) {
        // The channel already drops repeats; a second wait for the same id never shows a card.
        return;
      }
      const card: PendingServerRequest = {
        id,
        kind: request.kind,
        sessionId: request.session_id,
        request,
        replayed: meta.replayed,
        receivedAt: (this.options.now ?? Date.now)(),
      };
      this.waiting.set(id, { card, settle: resolve });
      meta.signal.addEventListener("abort", () => this.close(id), { once: true });
      this.changed();
    });
  }

  private close(id: string): void {
    if (!this.waiting.delete(id)) return;
    this.settled.add(id);
    if (this.settled.size > SETTLED_MEMORY) {
      const oldest = this.settled.values().next().value;
      if (oldest !== undefined) this.settled.delete(oldest);
    }
    this.changed();
  }

  private changed(): void {
    this.snapshot = [...this.waiting.values()].map((entry) => entry.card);
    this.bySession.clear();
    for (const listener of [...this.listeners]) listener();
  }
}

/**
 * A pending request with its answer bound to the store, the shape the cards take (slice C). The
 * store still decides: `answer` returns false for a second answer, a withdrawn request or an id
 * it never saw, so a double click or a re-offered card never sends twice.
 */
export interface OpenServerRequest extends PendingServerRequest {
  answer(answer: ServerRequestAnswer): boolean;
}

/** Binds each pending request of `list` to `store.answer`. */
export function bindAnswers(
  store: ServerRequestStore,
  list: readonly PendingServerRequest[],
): readonly OpenServerRequest[] {
  return list.map((card) => ({ ...card, answer: (answer) => store.answer(card.id, answer) }));
}
