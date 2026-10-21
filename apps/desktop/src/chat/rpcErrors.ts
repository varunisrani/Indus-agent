// Small helpers for reading call failures from the gateway client.

import { GatewayRpcError } from "@indus/client";
import type { ErrorName } from "@indus/protocol";

/** The protocol error name of a failed call (`SESSION_BUSY`, …), if it is one. */
export function errorName(error: unknown): string | undefined {
  return error instanceof GatewayRpcError ? error.errorName : undefined;
}

export function isError(error: unknown, name: ErrorName): boolean {
  return error instanceof GatewayRpcError && error.is(name);
}

/**
 * True when the call's outcome is unknown: a timeout or a dropped link may or may not have reached
 * the daemon. An error frame is the daemon's definite answer.
 */
export function isUncertain(error: unknown): boolean {
  return !(error instanceof GatewayRpcError);
}

export function errorMessage(error: unknown): string {
  if (error instanceof Error && error.message) return error.message;
  return String(error);
}
