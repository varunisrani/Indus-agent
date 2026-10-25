// Settings → Appearance → Theme, the P24-33 part (below the light / dark / system choice): the
// twelve presets and the person's own themes, an accent hue, a per-profile theme with chat and
// terminal fonts, the terminal UI sync, and the VS Code Marketplace import (search, download,
// convert, install, use). The choices live in `themeStore.ts`; `ThemeProvider` paints them.

import { presets } from "@indus/ui";
import { type FormEvent, useState, useSyncExternalStore } from "react";
import { desktopBridge } from "../app/bridge.ts";
import { useTheme } from "../app/theme.tsx";
import type { IndusDesktopThemes, IndusMarketplaceItem } from "../global.d.ts";
import { useT } from "../i18n/index.tsx";
import { bridgeRest, type RestLike } from "../settings/settingsApi.ts";
import { deskFromPresetCss } from "./deskTokens.ts";
import { type AppearanceStore, appearance, syncTui, userThemeFrom } from "./themeStore.ts";
import { base64Bytes, themesFromVsix } from "./vscodeImport.ts";

const BUTTON =
  "rounded border border-line px-2 py-0.5 text-xs hover:bg-raised disabled:opacity-40 focus-visible:outline-2 focus-visible:outline-focus";

export function useAppearance(store: AppearanceStore = appearance) {
  return useSyncExternalStore(store.subscribe, store.get, store.get);
}

/** A profile's own theme and fonts over the window-wide choice (inside the profile switcher). */
export function ProfileAppearanceStyle(props: { profile: string | null; store?: AppearanceStore }) {
  const store = props.store ?? appearance;
  const value = useAppearance(store);
  const { resolved } = useTheme();
  const own = props.profile !== null ? value.profiles[props.profile] : undefined;
  if (props.profile === null || own === undefined || Object.keys(own).length === 0) return null;
  const scheme = resolved === "indus-dark" ? "dark" : "light";
  const css = store.css(props.profile, scheme);
  if (css === "") return null;
  // a profile preset repaints the base colours; the desk surfaces follow them (W51-b)
  const derived = own.theme === undefined ? "" : `\n${deskFromPresetCss(scheme)}`;
  return <style data-indus-appearance={props.profile}>{`${css}${derived}`}</style>;
}

/** Search, download, convert and install a Marketplace theme (then use it). */
export async function installMarketplaceTheme(
  item: IndusMarketplaceItem,
  themes: Pick<IndusDesktopThemes, "downloadMarketplace">,
  store: AppearanceStore,
): Promise<string> {
  const origin = `${item.publisher}.${item.extension}`;
  const owned = store.get().userThemes.find((u) => u.origin === origin);
  if (owned !== undefined) {
    store.choose(`user:${owned.id}`);
    return owned.label;
  }
  const { vsixBase64 } = await themes.downloadMarketplace({
    publisher: item.publisher,
    extension: item.extension,
    version: item.version,
  });
  const [first] = await themesFromVsix(base64Bytes(vsixBase64));
  if (first === undefined) throw new Error("no colour theme in the package");
  const stored = userThemeFrom(first, origin);
  store.addUserTheme(stored);
  store.choose(`user:${stored.id}`);
  return stored.label;
}

export function AppearancePresets(props: {
  store?: AppearanceStore;
  themes?: Pick<IndusDesktopThemes, "searchMarketplace" | "downloadMarketplace"> | null;
  api?: RestLike | null;
  profile?: string | null;
  mode?: "light" | "dark";
}) {
  const t = useT();
  const store = props.store ?? appearance;
  const value = useAppearance(store);
  const themes = props.themes === undefined ? (desktopBridge()?.themes ?? null) : props.themes;
  const api = props.api === undefined ? bridgeRest() : props.api;
  const profile = props.profile ?? null;
  const [query, setQuery] = useState("");
  const [items, setItems] = useState<IndusMarketplaceItem[]>([]);
  const [note, setNote] = useState<{ text: string; error: boolean } | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const perProfile = profile !== null ? (value.profiles[profile] ?? {}) : {};

  const options = [
    { id: "", label: t("appearance.preset.builtIn") },
    ...presets.map((p) => ({ id: p.id, label: p.label })),
    ...value.userThemes.map((u) => ({
      id: `user:${u.id}`,
      label: t("appearance.preset.user", { name: u.label }),
    })),
    // the registry's `theme` area (P24-32): bundled and plugin themes, while their plugin is on
    ...store.contributed().map((c) => ({
      id: `contrib:${c.id}`,
      label: t("appearance.preset.contributed", { name: c.label }),
    })),
  ];

  const search = async (event: FormEvent) => {
    event.preventDefault();
    if (themes === null) return;
    try {
      setItems((await themes.searchMarketplace({ query: query.trim() })).items);
      setNote(null);
    } catch (error) {
      setNote({ text: error instanceof Error ? error.message : String(error), error: true });
    }
  };

  const install = async (item: IndusMarketplaceItem) => {
    if (themes === null || busy !== null) return;
    setBusy(item.extension);
    try {
      const name = await installMarketplaceTheme(item, themes, store);
      setNote({ text: t("appearance.import.installed", { name }), error: false });
    } catch (error) {
      setNote({
        text: t("appearance.import.failed", {
          message: error instanceof Error ? error.message : String(error),
        }),
        error: true,
      });
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="settings-rows" data-testid="appearance-presets">
      <label className="settings-row">
        <span className="settings-row-label">{t("appearance.preset")}</span>
        <select
          value={value.theme ?? ""}
          onChange={(event) => store.choose(event.target.value === "" ? null : event.target.value)}
        >
          {options.map((o) => (
            <option key={o.id} value={o.id}>
              {o.label}
            </option>
          ))}
        </select>
      </label>
      <label className="settings-row">
        <span className="settings-row-label">{t("appearance.accent")}</span>
        <input
          type="range"
          min={0}
          max={359}
          value={value.accentHue ?? 220}
          onChange={(event) => store.setAccent(Number(event.target.value))}
        />
        <button type="button" className={BUTTON} onClick={() => store.setAccent(null)}>
          {t("appearance.accent.reset")}
        </button>
      </label>
      {profile !== null ? (
        <>
          <label className="settings-row">
            <span className="settings-row-label">{t("appearance.profile")}</span>
            <select
              value={perProfile.theme ?? ""}
              onChange={(event) => store.setProfile(profile, { theme: event.target.value })}
            >
              <option value="">{t("appearance.profile.same")}</option>
              {options.slice(1).map((o) => (
                <option key={o.id} value={o.id}>
                  {o.label}
                </option>
              ))}
            </select>
          </label>
          <label className="settings-row">
            <span className="settings-row-label">{t("appearance.chatFont")}</span>
            <input
              value={perProfile.chatFont ?? ""}
              onChange={(event) => store.setProfile(profile, { chatFont: event.target.value })}
            />
          </label>
          <label className="settings-row">
            <span className="settings-row-label">{t("appearance.terminalFont")}</span>
            <input
              value={perProfile.terminalFont ?? ""}
              onChange={(event) => store.setProfile(profile, { terminalFont: event.target.value })}
            />
          </label>
        </>
      ) : null}
      <div className="settings-row">
        <button
          type="button"
          className={BUTTON}
          onClick={() =>
            void syncTui(api, profile, props.mode ?? "dark").then((ok) => {
              if (ok) setNote({ text: t("appearance.tuiSynced"), error: false });
            })
          }
        >
          {t("appearance.tuiSync")}
        </button>
      </div>
      {value.userThemes.length > 0 ? (
        <ul className="settings-row flex flex-col gap-1">
          {value.userThemes.map((u) => (
            <li key={u.id}>
              <button type="button" className={BUTTON} onClick={() => store.removeUserTheme(u.id)}>
                {t("appearance.remove", { name: u.label })}
              </button>
            </li>
          ))}
        </ul>
      ) : null}
      {themes !== null ? (
        <form className="settings-row flex flex-col gap-1" onSubmit={search}>
          <span className="settings-row-label">{t("appearance.import")}</span>
          <div className="flex gap-2">
            <input
              className="flex-1"
              aria-label={t("appearance.import.search")}
              placeholder={t("appearance.import.search")}
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
            <button type="submit" className={BUTTON}>
              {t("appearance.import.search")}
            </button>
          </div>
          <ul className="flex flex-col gap-1">
            {items.map((item) => (
              <li key={`${item.publisher}.${item.extension}`} className="flex items-center gap-2">
                <span className="flex-1">
                  {item.displayName}{" "}
                  <span className="text-xs text-muted">
                    {t("appearance.import.installs", { count: item.installs })}
                  </span>
                </span>
                <button
                  type="button"
                  className={BUTTON}
                  disabled={busy !== null}
                  onClick={() => void install(item)}
                >
                  {t("appearance.import.install")}
                </button>
              </li>
            ))}
          </ul>
        </form>
      ) : null}
      {note !== null ? (
        <p role={note.error ? "alert" : "status"} className="settings-row text-xs">
          {note.text}
        </p>
      ) : null}
    </div>
  );
}
