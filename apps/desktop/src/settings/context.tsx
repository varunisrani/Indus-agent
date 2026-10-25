// What every settings page reads: the REST proxy, the bridge groups Settings uses, the profile the
// "Applies to" chips name, and the desktop preferences.

import { createContext, useCallback, useContext, useEffect, useState } from "react";
import type {
  IndusDesktopAbout,
  IndusDesktopAmbient,
  IndusDesktopConnection,
  IndusDesktopExternal,
  IndusDesktopFs,
  IndusDesktopLogs,
  IndusDesktopNotifications,
  IndusDesktopPrefs,
  IndusDesktopProfile,
  IndusDesktopSecrets,
  IndusDesktopShortcuts,
  IndusDesktopUninstall,
  IndusDesktopUpdates,
  IndusDesktopZoom,
  IndusLaunchFacts,
  IndusPrefsPatch,
  IndusPrefsSnapshot,
} from "../global.d.ts";
import type { ViewId } from "./pages.ts";
import type { SchemaFields } from "./search.ts";
import type { RestLike } from "./settingsApi.ts";

export interface SettingsBridge {
  readonly prefs?: IndusDesktopPrefs;
  readonly zoom?: IndusDesktopZoom;
  readonly connection?: IndusDesktopConnection;
  readonly secrets?: IndusDesktopSecrets;
  readonly updates?: IndusDesktopUpdates;
  readonly profile?: IndusDesktopProfile;
  readonly facts?: IndusLaunchFacts;
  /** The tray's availability (P20-36). */
  readonly ambient?: IndusDesktopAmbient;
  /** The data-URL read cap (P20-29). */
  readonly fs?: IndusDesktopFs;
  /** About → Uninstall (P24-34). */
  readonly uninstall?: IndusDesktopUninstall;
  /** About → Licences: the packaged third-party notices (W46, F-2). */
  readonly about?: IndusDesktopAbout;
  // settings-b (W52-j)
  /** Quick entry's on/off and each global shortcut's registration state (P20-35). */
  readonly shortcuts?: IndusDesktopShortcuts;
  /** Notifications › Send test notification (P20-36). */
  readonly notifications?: IndusDesktopNotifications;
  /** Gateways › Diagnostics: show the desktop log (P20-39). */
  readonly logs?: IndusDesktopLogs;
  /** Providers › API keys: "Get a key" links open in the browser. */
  readonly external?: IndusDesktopExternal;
}

/** `model.save_key` over the daemon (keys go to the profile's secret store, never the renderer). */
export type SaveKey = (slug: string, apiKey: string) => Promise<void>;

export interface SettingsContextValue {
  readonly api: RestLike | null;
  readonly bridge: SettingsBridge | null;
  /** The chosen "Applies to" profile; null = the connection's own. */
  readonly profile: string | null;
  /** The app's active profile (what null means), when known. */
  readonly activeProfile: string | null;
  readonly saveKey: SaveKey | null;
  readonly openOnboarding: () => void;
  /** `GET /api/config` for the chosen profile (null while loading or unreadable). */
  readonly config: unknown;
  readonly reloadConfig: () => void;
  /** A hand-built row a link or search hit points at. */
  readonly focusRow: string | null;
  // -- settings slice A (W52-i): hand-built pages that also show schema rows --------------------
  /** `GET /api/config/schema` (null while loading or unreadable). */
  readonly schema?: SchemaFields | null;
  /** The schema field a link or search hit points at. */
  readonly focusField?: string | null;
  /** Write one field (`PUT /api/config`, the chosen profile); null when saved, else the error. */
  readonly saveField?: (key: string, value: unknown) => Promise<string | null>;
  /** Write a whole subtree (`PUT /api/config {config: patch}`); null when saved, else the error. */
  readonly savePatch?: (patch: Record<string, unknown>) => Promise<string | null>;
  /** Open another settings page (W52-j: cross-page links such as "Have an API key instead?"). */
  readonly go?: (target: { view: ViewId; page?: string; row?: string | null }) => void;
}

export const SettingsContext = createContext<SettingsContextValue | null>(null);

export function useSettings(): SettingsContextValue {
  const value = useContext(SettingsContext);
  if (value === null) throw new Error("useSettings outside the settings overlay");
  return value;
}

/** Run a bridge call; a refusal (`not-available: …` before its owner lands) becomes a value. */
export async function attempt<T>(
  call: (() => Promise<T>) | null | undefined,
): Promise<{ ok: true; value: T } | { ok: false; code: string; message: string }> {
  if (call === null || call === undefined) {
    return { ok: false, code: "not-available", message: "not available in this window" };
  }
  try {
    return { ok: true, value: await call() };
  } catch (error) {
    const text = error instanceof Error ? error.message : String(error);
    const cut = text.indexOf(": ");
    return cut > 0
      ? { ok: false, code: text.slice(0, cut), message: text.slice(cut + 2) }
      : { ok: false, code: "error", message: text };
  }
}

export interface PrefsHandle {
  readonly prefs: IndusPrefsSnapshot | null;
  /** Null while loading; a code when main cannot answer. */
  readonly error: string | null;
  update(patch: IndusPrefsPatch): Promise<boolean>;
}

/** The desktop preferences, kept current by `prefs.onChanged`. */
export function usePrefs(bridge: SettingsBridge | null): PrefsHandle {
  const [prefs, setPrefs] = useState<IndusPrefsSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const source = bridge?.prefs;
  useEffect(() => {
    let live = true;
    void attempt(source ? () => source.get() : null).then((answer) => {
      if (!live) return;
      if (answer.ok) setPrefs(answer.value);
      else setError(answer.code);
    });
    const off = source?.onChanged((next) => setPrefs(next));
    return () => {
      live = false;
      off?.();
    };
  }, [source]);
  const update = useCallback(
    async (patch: IndusPrefsPatch) => {
      const answer = await attempt(source ? () => source.set(patch) : null);
      if (answer.ok) {
        setPrefs(answer.value);
        setError(null);
        return true;
      }
      setError(answer.code);
      return false;
    },
    [source],
  );
  return { prefs, error, update };
}
