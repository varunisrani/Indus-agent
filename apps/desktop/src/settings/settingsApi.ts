// The REST calls Settings and onboarding make, through main's proxy (`api.request`, P20-14). Every
// call answers a value: `{ok: true, data}` or `{ok: false, status, code, message}` — a refusal,
// a 404 or a bridge error never throws into a page.

import { desktopBridge } from "../app/bridge.ts";
import type { IndusApiRequest, IndusApiResult } from "../global.d.ts";

export interface RestLike {
  request(payload: IndusApiRequest): Promise<IndusApiResult>;
}

export type RestAnswer<T> =
  | { readonly ok: true; readonly status: number; readonly data: T }
  | {
      readonly ok: false;
      readonly status: number | null;
      readonly code: string;
      readonly message: string;
    };

/** The bridge's REST proxy, or null without a main process (previews, tests). */
export function bridgeRest(): RestLike | null {
  return desktopBridge()?.api ?? null;
}

function detail(body: unknown): { code: string; message: string } | null {
  const raw = (body as { detail?: unknown } | null)?.detail;
  if (typeof raw === "string") return { code: "error", message: raw };
  if (raw !== null && typeof raw === "object") {
    const record = raw as { code?: unknown; message?: unknown };
    return {
      code: typeof record.code === "string" ? record.code : "error",
      message: typeof record.message === "string" ? record.message : "",
    };
  }
  return null;
}

/** "code: message" errors from the bridge become (code, message). */
function bridgeError(error: unknown): { code: string; message: string } {
  const text = error instanceof Error ? error.message : String(error);
  const cut = text.indexOf(": ");
  return cut > 0
    ? { code: text.slice(0, cut), message: text.slice(cut + 2) }
    : { code: "error", message: text };
}

export async function rest<T>(
  api: RestLike | null,
  method: IndusApiRequest["method"],
  path: string,
  options: { body?: unknown; profile?: string | null; timeoutMs?: number } = {},
): Promise<RestAnswer<T>> {
  if (api === null) {
    return { ok: false, status: null, code: "not-connected", message: "Indus is not connected" };
  }
  const payload: IndusApiRequest = {
    method,
    path,
    ...(options.body === undefined ? {} : { body: options.body }),
    ...(options.profile ? { profile: options.profile } : {}),
    ...(options.timeoutMs === undefined ? {} : { timeoutMs: options.timeoutMs }),
  };
  let result: IndusApiResult;
  try {
    result = await api.request(payload);
  } catch (error) {
    const { code, message } = bridgeError(error);
    return { ok: false, status: null, code, message };
  }
  if (result.status >= 200 && result.status < 300) {
    return { ok: true, status: result.status, data: result.body as T };
  }
  const found = detail(result.body);
  return {
    ok: false,
    status: result.status,
    code: found?.code ?? (result.kind === "not-found" ? "not-found" : "error"),
    message: found?.message || `HTTP ${result.status}`,
  };
}

/** `{a: {b: value}}` for the dotted key `a.b` (what `PUT /api/config` lays over the file). */
export function patchFor(key: string, value: unknown): Record<string, unknown> {
  const parts = key.split(".");
  let node: unknown = value;
  for (let i = parts.length - 1; i >= 0; i -= 1) node = { [parts[i] as string]: node };
  return node as Record<string, unknown>;
}

/** The value at a dotted key of a nested object, or undefined. */
export function valueAt(tree: unknown, key: string): unknown {
  let node: unknown = tree;
  for (const part of key.split(".")) {
    if (node === null || typeof node !== "object" || Array.isArray(node)) return undefined;
    node = (node as Record<string, unknown>)[part];
  }
  return node;
}

/** What `GET /api/config` shows instead of a stored credential (never send it back changed). */
export const SECRET_PLACEHOLDER = "••••••••";
