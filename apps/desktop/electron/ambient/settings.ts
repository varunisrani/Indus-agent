// Per-device ambient preferences (P20-36), owned by main because main must apply them on a cold
// launch before any window asks: `<userData>/ambient.json`. Garbage or a missing file means the
// defaults (tray off, keep awake while working, the offer not made yet, notifications and sounds on).

import fs from "node:fs";
import path from "node:path";
import { readJsonFile, writeFileAtomic } from "../fsSecure.ts";

export type KeepAwakeMode = "never" | "while-working" | "always";
export const KEEP_AWAKE_MODES: readonly KeepAwakeMode[] = ["never", "while-working", "always"];
export const DEFAULT_KEEP_AWAKE: KeepAwakeMode = "while-working";

export interface AmbientSettings {
  minimizeToTray: boolean;
  keepAwake: KeepAwakeMode;
  /** The one-time "keep awake while working" offer was shown on this device. */
  keepAwakeOffered: boolean;
  notifications: boolean;
  sounds: boolean;
}

export function defaultAmbientSettings(): AmbientSettings {
  return {
    minimizeToTray: false,
    keepAwake: DEFAULT_KEEP_AWAKE,
    keepAwakeOffered: false,
    notifications: true,
    sounds: true,
  };
}

export function parseKeepAwakeMode(value: unknown): KeepAwakeMode | null {
  return typeof value === "string" && (KEEP_AWAKE_MODES as readonly string[]).includes(value)
    ? (value as KeepAwakeMode)
    : null;
}

export function sanitizeAmbientSettings(raw: unknown): AmbientSettings {
  const settings = defaultAmbientSettings();
  if (raw === null || typeof raw !== "object") return settings;
  const record = raw as Record<string, unknown>;
  if (typeof record.minimizeToTray === "boolean") settings.minimizeToTray = record.minimizeToTray;
  settings.keepAwake = parseKeepAwakeMode(record.keepAwake) ?? settings.keepAwake;
  if (typeof record.keepAwakeOffered === "boolean")
    settings.keepAwakeOffered = record.keepAwakeOffered;
  if (typeof record.notifications === "boolean") settings.notifications = record.notifications;
  if (typeof record.sounds === "boolean") settings.sounds = record.sounds;
  return settings;
}

export class AmbientSettingsStore {
  private readonly file: string;
  private readonly log: (line: string) => void;
  private settings: AmbientSettings;

  constructor(file: string, log: (line: string) => void) {
    this.file = file;
    this.log = log;
    this.settings = sanitizeAmbientSettings(readJsonFile(file));
  }

  get(): AmbientSettings {
    return { ...this.settings };
  }

  /** Merge `patch`, write the file, and return the result (a failed write keeps it in memory). */
  update(patch: Partial<AmbientSettings>): AmbientSettings {
    this.settings = sanitizeAmbientSettings({ ...this.settings, ...patch });
    try {
      fs.mkdirSync(path.dirname(this.file), { recursive: true });
      writeFileAtomic(this.file, `${JSON.stringify(this.settings, null, 2)}\n`);
    } catch {
      this.log("[ambient] could not save the preferences; they apply until the app quits");
    }
    return this.get();
  }
}
