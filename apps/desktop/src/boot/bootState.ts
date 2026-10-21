// From main's boot snapshot and the installer's events to the one view the overlay shows.

import type { IndusBootProgress, IndusInstallerEvent } from "../global.d.ts";

export type StageState = "pending" | "running" | "succeeded" | "skipped" | "failed";

export interface InstallStage {
  readonly name: string;
  readonly title: string;
  readonly state: StageState;
  readonly error: string | null;
}

export interface InstallerState {
  readonly stages: readonly InstallStage[];
  readonly log: readonly string[];
  /** A manifest arrived and neither completion nor failure since. */
  readonly active: boolean;
  readonly failed: string | null;
  readonly complete: boolean;
}

/** Installer lines kept; the view shows the tail. */
export const INSTALL_LOG_KEEP = 200;
export const INSTALL_LOG_SHOWN = 12;

export const EMPTY_INSTALLER: InstallerState = {
  stages: [],
  log: [],
  active: false,
  failed: null,
  complete: false,
};

export function reduceInstaller(state: InstallerState, event: IndusInstallerEvent): InstallerState {
  switch (event.type) {
    case "manifest":
      return {
        stages: event.stages.map((stage) => ({
          name: stage.name,
          title: stage.title,
          state: "pending",
          error: null,
        })),
        log: [],
        active: true,
        failed: null,
        complete: false,
      };
    case "log": {
      const log = [...state.log, event.line];
      return { ...state, log: log.length > INSTALL_LOG_KEEP ? log.slice(-INSTALL_LOG_KEEP) : log };
    }
    case "stage": {
      const known = state.stages.some((stage) => stage.name === event.name);
      const stages = known
        ? state.stages.map((stage) =>
            stage.name === event.name
              ? { ...stage, state: event.state, error: event.error ?? null }
              : stage,
          )
        : [
            ...state.stages,
            { name: event.name, title: event.name, state: event.state, error: event.error ?? null },
          ];
      return { ...state, stages };
    }
    case "complete":
      return { ...state, active: false, complete: true };
    case "failed":
      return { ...state, active: false, failed: event.error };
  }
}

export type BootView =
  | { readonly kind: "ready" }
  | {
      readonly kind: "connecting";
      readonly phase: IndusBootProgress["phase"];
      readonly message: string;
      readonly progress: number;
    }
  | { readonly kind: "install"; readonly installer: InstallerState; readonly message: string }
  | { readonly kind: "first-run" }
  | {
      readonly kind: "failure";
      readonly message: string;
      readonly retryable: boolean;
      readonly latched: boolean;
    }
  | { readonly kind: "reauth"; readonly message: string };

export function bootView(progress: IndusBootProgress | null, installer: InstallerState): BootView {
  if (progress === null) return { kind: "connecting", phase: "resolve", message: "", progress: 0 };
  if (progress.phase === "ready") return { kind: "ready" };
  if (progress.phase === "error") {
    const message = progress.error ?? progress.message;
    if (progress.statusCode === 401 || progress.statusCode === 403)
      return { kind: "reauth", message };
    return {
      kind: "failure",
      message,
      retryable: progress.retryable === true,
      latched: progress.latched === true,
    };
  }
  if (installer.active) return { kind: "install", installer, message: progress.message };
  if (progress.phase === "first-run") return { kind: "first-run" };
  return {
    kind: "connecting",
    phase: progress.phase,
    message: progress.message,
    progress: progress.progress,
  };
}
