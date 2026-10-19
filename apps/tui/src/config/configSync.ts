// Display settings from the bridge: one `config.get full` when the session is ready, then a cheap
// `config.get mtime` poll every 5 s that re-reads and re-applies on change. Failures are quiet and
// keep what was applied; the next poll tries again.

import { type Clock, systemClock } from "@indus/client";
import type { ConfigGetResult } from "@indus/protocol";
import type { StoreApi } from "zustand";
import type { RpcLike } from "../cards/rpcLike.ts";
import { captureDestination, isCurrent } from "../state/destination.ts";
import { type BusyInputMode, type UiState, uiStore } from "../state/uiStore.ts";

export const POLL_MS = 5_000;
/** R05.18: in the TUI Enter queues while a reply runs unless the settings say otherwise. */
export const TUI_BUSY_DEFAULT: BusyInputMode = "queue";
export const PASTE_LINES_DEFAULT = 5;
export const PASTE_CHARS_DEFAULT = 2_000;

/** Settings that have no `UiState` field (yet); kept here for whoever needs them. */
export interface ConfigExtras {
  bellOnPrompt: boolean;
  bellOnComplete: boolean;
  /** Status bar segments to show; null = the default set. */
  statusBarFields: string[] | null;
  /** Per-section detail overrides (`tools`, `reasoning`, …). */
  detailSections: Record<string, string> | null;
  /** The MCP part of the settings; a change asks the bridge to reload MCP servers. */
  mcpRevision: string | null;
}

export const DEFAULT_EXTRAS: ConfigExtras = {
  bellOnPrompt: true,
  bellOnComplete: false,
  statusBarFields: null,
  detailSections: null,
  mcpRevision: null,
};

type Raw = Record<string, unknown>;

const isObject = (value: unknown): value is Raw =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const word = (value: unknown): string | null =>
  typeof value === "string" && value.trim() !== "" ? value.trim().toLowerCase() : null;

/** The first key present (not undefined) among `names`. */
const pick = (source: Raw, ...names: string[]): unknown => {
  for (const name of names) if (source[name] !== undefined) return source[name];
  return undefined;
};

export function normalizeBusy(raw: unknown): BusyInputMode {
  const value = word(raw);
  return value === "queue" || value === "steer" || value === "interrupt" ? value : TUI_BUSY_DEFAULT;
}

export function normalizeStatusBar(raw: unknown): UiState["statusBar"] {
  if (raw === false) return "off";
  const value = word(raw);
  if (value === "off" || value === "false" || value === "hidden") return "off";
  if (value === "bottom") return "bottom";
  return "top";
}

const MOUSE_OFF = new Set(["off", "false", "no", "0", "none"]);
const MOUSE_ALL = new Set(["on", "true", "yes", "1", "all", "full"]);

export function normalizeMouse(raw: unknown): string {
  if (raw === true || raw === 1) return "all";
  if (raw === false || raw === 0 || raw === null || raw === undefined) return "off";
  const value = word(raw);
  if (value === null || MOUSE_OFF.has(value)) return "off";
  if (MOUSE_ALL.has(value)) return "all";
  if (value === "wheel" || value === "scroll") return "wheel";
  if (value === "buttons" || value === "click") return "buttons";
  return "all";
}

export function normalizeIndicator(raw: unknown): string {
  const value = word(raw);
  return value === "dots" || value === "none" || value === "spinner" ? value : "spinner";
}

export function normalizeDetails(raw: unknown): UiState["detailsMode"] {
  const value = word(raw);
  return value === "expanded" || value === "hidden" ? value : "collapsed";
}

function count(raw: unknown, fallback: number): number {
  const value = typeof raw === "string" ? Number.parseInt(raw, 10) : raw;
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? Math.round(value)
    : fallback;
}

const bool = (raw: unknown, fallback: boolean): boolean =>
  typeof raw === "boolean" ? raw : fallback;

function fieldList(raw: unknown): string[] | null {
  if (!Array.isArray(raw)) return null;
  const names = raw.map((v) => String(v).trim().toLowerCase()).filter(Boolean);
  return names.length > 0 ? names : null;
}

function sections(raw: unknown): Record<string, string> | null {
  if (!isObject(raw)) return null;
  const entries = Object.entries(raw).filter(
    (e): e is [string, string] => typeof e[1] === "string",
  );
  return entries.length > 0 ? Object.fromEntries(entries) : null;
}

/** Turn a `config.get full` value into a store patch and the extras. */
export function displaySettings(full: unknown): { patch: Partial<UiState>; extras: ConfigExtras } {
  const top = isObject(full) ? full : {};
  // Accept a nested `display` / `tui` block as well as the bridge's flat keys; flat keys win.
  const source: Raw = {
    ...(isObject(top.display) ? top.display : {}),
    ...(isObject(top.tui) ? top.tui : {}),
    ...top,
  };
  const patch: Partial<UiState> = {
    busyInputMode: normalizeBusy(pick(source, "busy", "busy_input_mode")),
    statusBar: normalizeStatusBar(pick(source, "statusbar", "status_bar", "tui_statusbar")),
    indicatorStyle: normalizeIndicator(pick(source, "indicator", "tui_status_indicator")),
    detailsMode: normalizeDetails(pick(source, "details", "details_mode")),
    showReasoning: bool(source.show_reasoning, false),
    streaming: bool(source.streaming, true),
    timestamps: bool(source.timestamps, false),
    inlineDiffs: bool(source.inline_diffs, true),
    mousePreset: normalizeMouse(pick(source, "mouse", "mouse_tracking")),
    pasteLines: count(pick(source, "paste_lines", "paste_collapse_threshold"), PASTE_LINES_DEFAULT),
    pasteChars: count(
      pick(source, "paste_chars", "paste_collapse_char_threshold"),
      PASTE_CHARS_DEFAULT,
    ),
  };
  const theme = word(source.theme);
  if (theme !== null) patch.themeName = theme;
  const revision = pick(source, "mcp_revision", "mcp_rev");
  const extras: ConfigExtras = {
    bellOnPrompt: bool(source.bell_on_prompt, DEFAULT_EXTRAS.bellOnPrompt),
    bellOnComplete: bool(source.bell_on_complete, DEFAULT_EXTRAS.bellOnComplete),
    statusBarFields: fieldList(pick(source, "status_bar_fields", "statusbar_fields")),
    detailSections: sections(pick(source, "sections", "detail_sections")),
    mcpRevision:
      typeof revision === "string" || typeof revision === "number" ? String(revision) : null,
  };
  return { patch, extras };
}

export interface ConfigSyncOptions {
  rpc: RpcLike;
  ui?: StoreApi<UiState>;
  clock?: Clock;
  pollMs?: number;
  onExtras?(extras: ConfigExtras): void;
  /** A line for the activity feed after an MCP reload. */
  onMcpReloaded?(message: string): void;
}

export class ConfigSync {
  private readonly rpc: RpcLike;
  private readonly ui: StoreApi<UiState>;
  private readonly clock: Clock;
  private readonly pollMs: number;
  private readonly options: ConfigSyncOptions;
  private first: Promise<void> | null = null;
  private timer: unknown = null;
  private running = false;
  private stamp: number | null = null;
  /** The MCP revision the bridge last took; null until the first `full` read. */
  private acceptedRevision: string | null = null;
  private reloading = false;
  private current: ConfigExtras = { ...DEFAULT_EXTRAS };

  constructor(options: ConfigSyncOptions) {
    this.options = options;
    this.rpc = options.rpc;
    this.ui = options.ui ?? uiStore;
    this.clock = options.clock ?? systemClock;
    this.pollMs = options.pollMs ?? POLL_MS;
  }

  get extras(): ConfigExtras {
    return this.current;
  }

  /** Read and apply once, then poll. Calling it again returns the first read. */
  start(): Promise<void> {
    if (this.first === null) {
      this.running = true;
      this.first = Promise.all([this.readStamp().then((s) => this.seed(s)), this.refresh()]).then(
        () => this.arm(),
      );
    }
    return this.first;
  }

  stop(): void {
    this.running = false;
    if (this.timer !== null) this.clock.clearTimeout(this.timer);
    this.timer = null;
  }

  /** One poll: re-read `full` when the stamp moved; retry a pending MCP reload. */
  async poll(): Promise<void> {
    const stamp = await this.readStamp();
    if (stamp === undefined) return;
    if (this.stamp === null) {
      this.seed(stamp);
      return;
    }
    if (stamp !== null && stamp !== this.stamp) {
      if (await this.refresh()) this.stamp = stamp;
      return;
    }
    await this.syncMcp();
  }

  private seed(stamp: number | null | undefined): void {
    if (typeof stamp === "number" && this.stamp === null) this.stamp = stamp;
  }

  private arm(): void {
    if (!this.running) return;
    this.timer = this.clock.setTimeout(() => {
      this.timer = null;
      void this.poll().finally(() => this.arm());
    }, this.pollMs);
  }

  private params(key: string): object {
    const sid = this.ui.getState().sid;
    return sid === null ? { key } : { key, session_id: sid };
  }

  /** The settings file stamp; null when there is none, undefined when the call failed. */
  private async readStamp(): Promise<number | null | undefined> {
    try {
      const result = (await this.rpc.call(
        "config.get",
        this.params("mtime"),
      )) as ConfigGetResult | null;
      return typeof result?.value === "number" ? result.value : null;
    } catch {
      return undefined;
    }
  }

  /** Read `full` and apply it; false when the call failed or the session moved meanwhile. */
  private async refresh(): Promise<boolean> {
    const destination = captureDestination();
    let result: ConfigGetResult | null;
    try {
      result = (await this.rpc.call("config.get", this.params("full"))) as ConfigGetResult | null;
    } catch {
      return false;
    }
    if (!isCurrent(destination) || !isObject(result?.value)) return false;
    const { patch, extras } = displaySettings(result.value);
    this.ui.getState().patch(patch);
    this.current = extras;
    this.options.onExtras?.(extras);
    if (this.acceptedRevision === null) this.acceptedRevision = extras.mcpRevision ?? "";
    else await this.syncMcp();
    return true;
  }

  private async syncMcp(): Promise<void> {
    const wanted = this.current.mcpRevision;
    if (wanted === null || this.acceptedRevision === null) return;
    if (wanted === this.acceptedRevision || this.reloading) return;
    this.reloading = true;
    try {
      const sid = this.ui.getState().sid;
      const result = (await this.rpc.call(
        "reload.mcp",
        sid === null ? {} : { session_id: sid },
      )) as {
        message?: string;
      } | null;
      this.acceptedRevision = wanted;
      this.options.onMcpReloaded?.(result?.message || "MCP servers reloaded.");
    } catch {
      // Not accepted: the next poll asks again.
    } finally {
      this.reloading = false;
    }
  }
}
