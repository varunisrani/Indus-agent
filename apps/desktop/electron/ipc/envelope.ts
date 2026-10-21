// The answer shape of every invoke and sync channel. Handlers return values or throw; the
// registry wraps either into an envelope, and the preload unwraps it (a value, or an `Error`
// whose message starts with the code). Expected failures therefore never reach Electron as a
// rejected handler (which logs a stack per call) and the renderer can tell them apart.
//
// Plain module: the preload bundles it.

export type IpcErrorCode =
  | "sender-rejected"
  | "bad-request"
  | "not-available"
  | "internal"
  | (string & { readonly brand?: "handler-code" });

export type Envelope<T> =
  | { ok: true; value: T }
  | { ok: false; code: IpcErrorCode; message: string };

/** Throw from a handler to answer `{ ok: false, code, message }`. */
export class IpcError extends Error {
  readonly code: string;

  constructor(code: string, message: string) {
    super(message);
    this.name = "IpcError";
    this.code = code;
  }
}

export const SENDER_REJECTED_MESSAGE = "this frame may not use the desktop bridge";

export function okEnvelope<T>(value: T): Envelope<T> {
  return { ok: true, value };
}

export function errorEnvelope(code: IpcErrorCode, message: string): Envelope<never> {
  return { ok: false, code, message };
}

export function isEnvelope(value: unknown): value is Envelope<unknown> {
  if (value === null || typeof value !== "object") return false;
  const record = value as Record<string, unknown>;
  if (record.ok === true) return "value" in record;
  return (
    record.ok === false && typeof record.code === "string" && typeof record.message === "string"
  );
}

/** The value inside `answer`, or throw `Error("<code>: <message>")`. */
export function unwrapEnvelope(answer: unknown): unknown {
  if (!isEnvelope(answer)) throw new Error("internal: malformed answer from the desktop app");
  if (answer.ok) return answer.value;
  const error = new Error(`${answer.code}: ${answer.message}`);
  error.name = "IndusBridgeError";
  throw error;
}
