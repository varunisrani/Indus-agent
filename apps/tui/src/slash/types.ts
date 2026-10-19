// What slash commands can reach: the RPC context, the transcript, the composer and the overlay,
// plus optional hooks owned by other parts of the renderer.

import type { CatalogEntry } from "@indus/protocol";
import type { StoreApi } from "zustand";
import type { ConfirmOptions } from "../cards/openCards.ts";
import type { RpcLike } from "../cards/rpcLike.ts";
import type { OverlayState } from "../state/overlayStore.ts";
import type { UiState } from "../state/uiStore.ts";

export interface SlashContext {
  rpc: RpcLike;
  ui: StoreApi<UiState>;
  overlay: StoreApi<OverlayState>;
  /** One line (or a few) in the transcript, not sent to the model. */
  systemLine(text: string): void;
  /** Long text in the pager. */
  page(text: string, title?: string): void;
  /** Send a prompt now; `shown` replaces the body in the transcript (skills). */
  submit(body: string, shown?: string): void;
  /** Put text into the composer. */
  prefill(text: string): void;
  confirm(options: ConfirmOptions): Promise<boolean>;
  /** Leave the renderer with this exit code (42 asks the launcher to update). */
  exit(code: number): void;
  /** The `commands.catalog` list, once loaded. */
  catalog(): readonly CatalogEntry[] | null;
  env: Readonly<Record<string, string | undefined>>;
  later(run: () => void, ms: number): void;

  // Hooks other slices provide; a missing one makes its command say it is not available yet.
  newSession?(title?: string): void;
  switchSession?(sessionId: string): void;
  openSessions?(): void;
  redraw?(): void;
  copy?(arg: string): void;
  paste?(): void;
  openEditor?(seed: string): void;
  logLines?(): readonly string[];
  transcriptText?(): string;
  enqueue?(text: string): void;
  queued?(): readonly string[];
  writeFile?(path: string, text: string): Promise<void>;
}

/** Per-run view handed to a command. */
export interface RunContext extends SlashContext {
  /** The session at the time the command was typed. */
  readonly sid: string | null;
  /** True once a newer command ran or the session changed: drop late results (R05.10). */
  stale(): boolean;
  /** Run the command on the bridge (`slash.exec`, then `command.dispatch` if not owned). */
  forward(name: string, arg: string): Promise<void>;
  /** Show command output: a line when short, the pager when long. */
  show(text: string, title: string): void;
}

export interface LocalCommand {
  name: string;
  aliases?: readonly string[];
  /** One line for /help. */
  summary: string;
  args?: string;
  run(arg: string, ctx: RunContext, typed: string): void | Promise<void>;
}
