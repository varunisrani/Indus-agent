// Typed failures of a call made through @indus/client.

import { ERROR_CODES, type ErrorName } from "@indus/protocol";

const NAME_BY_CODE: ReadonlyMap<number, ErrorName> = new Map(
  (Object.entries(ERROR_CODES) as Array<[ErrorName, { code: number }]>).map(([name, row]) => [
    row.code,
    name,
  ]),
);

/** The symbolic name for a protocol error code, or undefined when the table has no such code. */
export function errorNameFor(code: number): ErrorName | undefined {
  return NAME_BY_CODE.get(code);
}

/** Codes the generated table does not declare: a server bug or a newer server (R05.15). */
export function isKnownErrorCode(code: number): boolean {
  return NAME_BY_CODE.has(code);
}

/** The server answered a call with an error frame. */
export class GatewayRpcError extends Error {
  override readonly name = "GatewayRpcError";
  readonly code: number;
  readonly data: unknown;
  /** Symbolic name: `data.name` when the server sent one, else the table's name for the code. */
  readonly errorName: string | undefined;
  /** False when the code is not in the generated error table. */
  readonly known: boolean;

  constructor(code: number, message: string, data?: unknown) {
    super(message);
    this.code = code;
    this.data = data;
    const sent = (data as { name?: unknown } | null | undefined)?.name;
    this.errorName = typeof sent === "string" ? sent : errorNameFor(code);
    this.known = isKnownErrorCode(code);
  }

  /** True when this error carries the given symbolic protocol error. */
  is(name: ErrorName): boolean {
    return this.code === ERROR_CODES[name].code;
  }
}

/** Build the typed error from a raw `error` member, tolerating a malformed one. */
export function rpcErrorFromWire(raw: unknown): GatewayRpcError {
  const body = (typeof raw === "object" && raw !== null ? raw : {}) as {
    code?: unknown;
    message?: unknown;
    data?: unknown;
  };
  const code = typeof body.code === "number" ? body.code : ERROR_CODES.INTERNAL_ERROR.code;
  const message = typeof body.message === "string" && body.message ? body.message : "call failed";
  return new GatewayRpcError(code, message, body.data);
}

/** The call waited longer than its timeout. */
export class RpcTimeout extends Error {
  override readonly name = "RpcTimeout";
  readonly method: string;
  readonly timeoutMs: number;

  constructor(method: string, timeoutMs: number) {
    super(`${method}: no answer within ${timeoutMs} ms`);
    this.method = method;
    this.timeoutMs = timeoutMs;
  }
}

/** The caller's abort signal fired before the answer came. */
export class RequestAborted extends Error {
  override readonly name = "RequestAborted";

  constructor(method: string) {
    super(`${method}: aborted by the caller`);
  }
}

/** A newer transport took over; calls sent on the old one will never be answered. */
export class TransportReplaced extends Error {
  override readonly name = "TransportReplaced";

  constructor() {
    super("the connection was replaced before the answer came");
  }
}

/** The transport closed while the call was waiting. */
export class TransportClosed extends Error {
  override readonly name = "TransportClosed";
  readonly reason: string;

  constructor(reason: string) {
    super(`the connection closed before the answer came (${reason})`);
    this.reason = reason;
  }
}

/** A call was made while no transport is attached. */
export class NotConnected extends Error {
  override readonly name = "NotConnected";

  constructor() {
    super("not connected to the agent");
  }
}
