// Agent-driven preview automation (P24-25): the window side of the `preview.act` /
// `preview.read` server requests.
//
// The window that shows a session's preview answers them for that session; any other session's
// request — or one arriving while no preview guest is attached — is declined with `not_shown`
// (P17-07), so the daemon offers it to the next window. Every action runs inside the guest page
// through `executeJavaScript` (the guest API of `annotate.ts`, installed on demand), never in the
// app's renderer, with the arguments passed as JSON. Navigation stays inside the guest
// (`loadURL` on the webview) and accepts http(s) and `file:///` only; nothing here opens the OS
// browser (D-16.20: the guest preload hands off trusted clicks only, and these clicks are not).
//
// Interface until the protocol carries the two kinds: `handle(request)` takes the server
// request's fields and resolves to the answer object, or to `{ declined: "not_shown" }`.

import { guestCall, guestInstallScript } from "./annotate.ts";
import { isWebUrl } from "./target.ts";

export const ACT_TIMEOUT_MS = 15_000;

export type PreviewRequestKind = "preview.act" | "preview.read";

export interface PreviewRequest {
  readonly kind: PreviewRequestKind;
  readonly request_id: string;
  readonly session_id: string;
  readonly action?: string;
  readonly what?: string;
  readonly url?: string;
  readonly selector?: string;
  readonly text?: string;
  readonly submit?: boolean;
  readonly dx?: number;
  readonly dy?: number;
  readonly pins?: readonly { selector: string; note: string }[];
}

export type PreviewAnswer =
  | { readonly declined: "not_shown" }
  | ({ readonly ok: boolean } & Readonly<Record<string, unknown>>);

/** What the pane hands over for the guest it shows. */
export interface PreviewGuestHandle {
  executeJavaScript(code: string): Promise<unknown>;
  loadURL(url: string): Promise<void>;
  /** The session whose preview this is (null: a preview nobody's agent may drive). */
  sessionId(): string | null;
}

const NOT_SHOWN: PreviewAnswer = { declined: "not_shown" };
const ACTIONS: ReadonlySet<string> = new Set(["click", "type", "scroll", "annotate"]);

function navigable(url: string): boolean {
  if (isWebUrl(url)) return true;
  try {
    const parsed = new URL(url);
    return parsed.protocol === "file:" && (parsed.host === "" || parsed.host === "localhost");
  } catch {
    return false;
  }
}

function withTimeout<T>(work: Promise<T>, ms: number): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(
      () => reject(new Error(`no answer from the page within ${ms} ms`)),
      ms,
    );
    work.then(
      (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      (error: unknown) => {
        clearTimeout(timer);
        reject(error);
      },
    );
  });
}

export class PreviewAutomation {
  private guest: PreviewGuestHandle | null = null;
  private readonly timeoutMs: number;

  constructor(options: { timeoutMs?: number } = {}) {
    this.timeoutMs = options.timeoutMs ?? ACT_TIMEOUT_MS;
  }

  attach(guest: PreviewGuestHandle): () => void {
    this.guest = guest;
    return () => {
      if (this.guest === guest) this.guest = null;
    };
  }

  /** The session this window can answer for now, or null. */
  shownSession(): string | null {
    return this.guest?.sessionId() ?? null;
  }

  async handle(request: PreviewRequest): Promise<PreviewAnswer> {
    const guest = this.guest;
    if (guest === null || guest.sessionId() === null || guest.sessionId() !== request.session_id) {
      return NOT_SHOWN;
    }
    try {
      if (request.kind === "preview.read") {
        if ((request.what ?? "snapshot") !== "snapshot") {
          return { ok: false, error: `cannot read ${String(request.what)}` };
        }
        return await this.call(guest, guestCall("snapshot"));
      }
      const action = request.action ?? "";
      if (action === "navigate") {
        const url = (request.url ?? "").trim();
        if (!navigable(url)) return { ok: false, error: "only http(s) and file:/// addresses" };
        await withTimeout(guest.loadURL(url), this.timeoutMs);
        return { ok: true, url };
      }
      if (!ACTIONS.has(action)) return { ok: false, error: `unknown action ${action}` };
      const payload: Record<string, unknown> = { action };
      for (const key of ["selector", "text", "submit", "dx", "dy", "pins"] as const) {
        if (request[key] !== undefined) payload[key] = request[key];
      }
      return await this.call(guest, guestCall("act", payload));
    } catch (error) {
      return { ok: false, error: error instanceof Error ? error.message : String(error) };
    }
  }

  private async call(guest: PreviewGuestHandle, code: string): Promise<PreviewAnswer> {
    await withTimeout(guest.executeJavaScript(guestInstallScript()), this.timeoutMs);
    const answer = await withTimeout(guest.executeJavaScript(code), this.timeoutMs);
    if (answer === null || typeof answer !== "object") {
      return { ok: false, error: "the page gave no answer" };
    }
    return { ok: true, ...(answer as Record<string, unknown>) } as PreviewAnswer;
  }
}

/** This window's automation host (the preview pane attaches its guest). */
export const windowPreviewAutomation = new PreviewAutomation();
