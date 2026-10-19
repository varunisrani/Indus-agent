// Session-level renderer state: which session is focused, what it is doing, and the display
// preferences with their TUI defaults (research 05 §3.27). One vanilla store plus a React hook.

import { useStore } from "zustand";
import { createStore, type StoreApi } from "zustand/vanilla";

export type BusyInputMode = "queue" | "steer" | "interrupt";
export type DetailsMode = "collapsed" | "expanded" | "hidden";
export type StatusBarPlace = "top" | "bottom" | "off";

/** A short message in the status area that is not part of the conversation. */
export interface Notice {
  id: number;
  key: string | null;
  level: "info" | "warning" | "error";
  text: string;
}

export interface UiState {
  sid: string | null;
  storedSid: string | null;
  /** Grows on every session switch; part of every captured destination (R05.10). */
  revision: number;
  /** The latest `session.info` payload. */
  info: Record<string, unknown> | null;
  busy: boolean;
  statusText: string;
  busyInputMode: BusyInputMode;
  detailsMode: DetailsMode;
  statusBar: StatusBarPlace;
  streaming: boolean;
  showReasoning: boolean;
  inlineDiffs: boolean;
  timestamps: boolean;
  indicatorStyle: string;
  mousePreset: string;
  pasteLines: number;
  pasteChars: number;
  themeName: string;
  usage: Record<string, unknown> | null;
  /** Additions to the wave-8 contract. */
  title: string | null;
  notice: Notice | null;
  compacting: boolean;
  /** False after the bridge child exited until the next ready frame. */
  connected: boolean;
  patch(p: Partial<UiState>): void;
}

export const STATUS_READY = "ready";
export const STATUS_WORKING = "working…";
export const STATUS_STARTING = "starting…";

type UiData = Omit<UiState, "patch">;

/** The values a fresh renderer starts with. */
export function uiDefaults(): UiData {
  return {
    sid: null,
    storedSid: null,
    revision: 0,
    info: null,
    busy: false,
    statusText: STATUS_STARTING,
    busyInputMode: "queue",
    detailsMode: "collapsed",
    statusBar: "top",
    streaming: true,
    showReasoning: false,
    inlineDiffs: true,
    timestamps: false,
    indicatorStyle: "dots",
    mousePreset: "all",
    pasteLines: 5,
    pasteChars: 2_000,
    themeName: "auto",
    usage: null,
    title: null,
    notice: null,
    compacting: false,
    connected: false,
  };
}

/** A config value for the busy-input mode; anything absent or unknown keeps the TUI's `queue`. */
export function busyInputModeFrom(raw: unknown): BusyInputMode {
  return raw === "steer" || raw === "interrupt" || raw === "queue" ? raw : "queue";
}

export const uiStore: StoreApi<UiState> = createStore<UiState>()((set) => ({
  ...uiDefaults(),
  patch: (p) => set(p),
}));

export function useUi<T>(sel: (s: UiState) => T): T {
  return useStore(uiStore, sel);
}

/** Back to the start values (tests, full teardown). */
export function resetUi(): void {
  uiStore.setState(uiDefaults());
}
