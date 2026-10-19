// The part of the renderer's RPC context the cards, pickers, slash dispatch and config sync use.
// R1's `Rpc` (app/rpc.ts) satisfies it structurally.

export interface RpcLike {
  call(method: string, params: unknown): Promise<unknown>;
}

/** The message of whatever a failed call rejected with. */
export const errorText = (error: unknown): string =>
  error instanceof Error ? error.message : String(error);
