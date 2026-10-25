// Settings › Appearance (W52-i): General, Theme (the preset gallery), Typography, Window & layout,
// Chat display and Pet. The choices live where they lived before: the theme store, desktop prefs,
// the zoom bridge, the layout store and the gateway's pet methods. The renderer-only display rows
// are desktop prefs too (W55-F3); reference rows with no feature behind them show disabled with
// the "not available" caption (W52-dsa-9 / -11).

import { presets, presetTheme } from "@indus/ui";
import { Monitor, Moon, PawPrint, Search, Sun, Trash2 } from "lucide-react";
import { type CSSProperties, useEffect, useMemo, useState, useSyncExternalStore } from "react";
import { desktopBridge } from "../app/bridge.ts";
import { Segmented } from "../app/overlay/OverlayFrame.tsx";
import { useProvidedShellChrome } from "../app/Shell.tsx";
import { useTheme } from "../app/theme.tsx";
import { useGatewayConnection } from "../gateway/react.tsx";
import type { IndusMarketplaceItem, IndusPrefsSnapshot } from "../global.d.ts";
import { type MessageKey, useT } from "../i18n/index.tsx";
import type { InterfaceMode } from "../panes/presets.ts";
import { useSwitcher } from "../profiles/switcherReact.tsx";
import { installMarketplaceTheme, useAppearance } from "../theme/AppearancePage.tsx";
import { appearance, syncTui } from "../theme/themeStore.ts";
import { attempt, type PrefsHandle, usePrefs, useSettings } from "./context.tsx";
import { PlaceholderSegmentedRow, PlaceholderSwitchRow, Slider, StatusNote } from "./controlsA.tsx";
import { DISPLAY_DEFAULTS } from "./displayPrefs.ts";
import {
  CompactSelect,
  OutlineButton,
  PageBody,
  SettingRow,
  SettingsEmpty,
  Switch,
  TextButton,
} from "./primitives.tsx";

// -- display preference rows (W55-F3) -------------------------------------------------------------------

type SwitchField =
  | "introSplash"
  | "reopenLastChat"
  | "chatBackdrop"
  | "hideTimeline"
  | "collapseThinking"
  | "fileBrowser"
  | "floatingComposer";
type ChoiceField =
  | "sessionDensity"
  | "tabStrip"
  | "textDirection"
  | "toolCallDisplay"
  | "appActionsSide";

/** A desktop-pref switch: live once main answered, the "not available" caption when it cannot. */
function PrefSwitchRow(props: {
  handle: PrefsHandle;
  rowId: string;
  field: SwitchField;
  title: string;
  description: string;
}) {
  const t = useT();
  const { focusRow } = useSettings();
  const { prefs, error, update } = props.handle;
  return (
    <SettingRow
      rowId={props.rowId}
      highlighted={focusRow === props.rowId}
      title={props.title}
      description={
        prefs === null && error !== null ? t("settings.notAvailable") : props.description
      }
      control={
        <Switch
          label={props.title}
          checked={prefs?.[props.field] ?? DISPLAY_DEFAULTS[props.field]}
          disabled={prefs === null}
          onChange={(checked) => void update({ [props.field]: checked })}
        />
      }
    />
  );
}

/** A desktop-pref choice as a segmented control. */
function PrefChoiceRow<F extends ChoiceField>(props: {
  handle: PrefsHandle;
  rowId: string;
  field: F;
  title: string;
  description: string;
  options: ReadonlyArray<{ value: IndusPrefsSnapshot[F]; label: string }>;
}) {
  const t = useT();
  const { focusRow } = useSettings();
  const { prefs, error, update } = props.handle;
  return (
    <SettingRow
      rowId={props.rowId}
      highlighted={focusRow === props.rowId}
      title={props.title}
      description={
        prefs === null && error !== null ? t("settings.notAvailable") : props.description
      }
      control={
        <fieldset className="settingsA-fieldset" disabled={prefs === null}>
          <Segmented<string>
            label={props.title}
            value={prefs?.[props.field] ?? DISPLAY_DEFAULTS[props.field]}
            options={props.options}
            onChange={(next) => void update({ [props.field]: next })}
          />
        </fieldset>
      }
    />
  );
}

// -- General -----------------------------------------------------------------------------------------

export function AppearanceGeneralPage() {
  const t = useT();
  const { focusRow, bridge } = useSettings();
  const handle = usePrefs(bridge);
  return (
    <PageBody>
      <SettingRow
        rowId="language"
        highlighted={focusRow === "language"}
        title={t("settingsA.general.language")}
        description={t("settingsA.general.languageBody")}
        control={
          <CompactSelect
            label={t("settingsA.general.language")}
            value="en"
            disabled
            width={128}
            options={[{ value: "en", label: "English" }]}
            onChange={() => undefined}
          />
        }
      />
      <PrefSwitchRow
        handle={handle}
        rowId="intro-splash"
        field="introSplash"
        title={t("settingsA.general.splash")}
        description={t("settingsA.general.splashBody")}
      />
      {/* no price data reaches the model picker (needs pricing from the backend) */}
      <PlaceholderSwitchRow title={t("settingsA.general.pricing")} />
      <PrefSwitchRow
        handle={handle}
        rowId="reopen-last-chat"
        field="reopenLastChat"
        title={t("settingsA.general.reopen")}
        description={t("settingsA.general.reopenBody")}
      />
      {/* no tips and no tours exist in this build */}
      <PlaceholderSwitchRow title={t("settingsA.general.tips")} />
      <PlaceholderSwitchRow title={t("settingsA.general.tours")} />
    </PageBody>
  );
}

// -- Theme ---------------------------------------------------------------------------------------------

interface Swatches {
  readonly canvas: string;
  readonly band: string;
  readonly line: string;
  readonly fg: string;
  readonly muted: string;
  readonly bubble: string;
}

interface ThemeCard {
  readonly id: string | null;
  readonly key: string;
  readonly label: string;
  readonly description: string;
  readonly swatches: Swatches | null;
  readonly removable: string | null;
}

function swatchesFrom(colour: (name: string) => string | undefined): Swatches | null {
  const canvas = colour("bg");
  const fg = colour("fg");
  if (canvas === undefined || fg === undefined) return null;
  return {
    canvas,
    band: colour("bg-raised") ?? canvas,
    line: colour("border") ?? fg,
    fg,
    muted: colour("fg-muted") ?? fg,
    bubble: colour("accent") ?? fg,
  };
}

function ThemePreview(props: { swatches: Swatches | null }) {
  const s = props.swatches;
  const style = (
    s === null
      ? {}
      : {
          "--tp-canvas": s.canvas,
          "--tp-band": s.band,
          "--tp-line": s.line,
          "--tp-fg": s.fg,
          "--tp-muted": s.muted,
          "--tp-bubble": s.bubble,
        }
  ) as CSSProperties;
  return (
    <span className="settingsA-preview" style={style} aria-hidden="true">
      <span className="settingsA-preview-band" />
      <span className="settingsA-preview-title" />
      <span className="settingsA-preview-line" />
      <span className="settingsA-preview-bubble" />
    </span>
  );
}

const MODES = [
  { value: "light", key: "theme.light", icon: <Sun aria-hidden="true" /> },
  { value: "dark", key: "theme.dark", icon: <Moon aria-hidden="true" /> },
  { value: "system", key: "theme.system", icon: <Monitor aria-hidden="true" /> },
] as const;

export function AppearanceThemePage() {
  const t = useT();
  const { mode, setMode, resolved } = useTheme();
  const { focusRow, api } = useSettings();
  const value = useAppearance(appearance);
  const scheme = resolved === "indus-dark" ? "dark" : "light";
  const profile = useSwitcher()?.getState().foreground ?? null;
  const themes = desktopBridge()?.themes ?? null;
  const [query, setQuery] = useState("");
  const [items, setItems] = useState<IndusMarketplaceItem[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [note, setNote] = useState<{ text: string; error: boolean } | null>(null);

  const cards = useMemo<ThemeCard[]>(() => {
    const out: ThemeCard[] = presets.map((p) => {
      const theme = presetTheme(p.id, scheme);
      return {
        id: p.id === "indus" ? null : p.id,
        key: p.id,
        label: p.label,
        description: t(`settingsA.theme.desc.${p.id}` as MessageKey),
        swatches: swatchesFrom((name) => theme[name as keyof typeof theme]?.hex),
        removable: null,
      };
    });
    for (const u of value.userThemes) {
      out.push({
        id: `user:${u.id}`,
        key: `user:${u.id}`,
        label: u.label,
        description:
          u.origin === null ? t("settingsA.theme.yours") : t("settingsA.theme.marketplace"),
        swatches: swatchesFrom((name) => u.colours[name as keyof typeof u.colours]),
        removable: u.id,
      });
    }
    for (const c of appearance.contributed()) {
      const theme = scheme === "dark" ? c.dark : c.light;
      out.push({
        id: `contrib:${c.id}`,
        key: `contrib:${c.id}`,
        label: c.label,
        description: t("settingsA.theme.plugin"),
        swatches: swatchesFrom((name) => theme[name as keyof typeof theme]?.hex),
        removable: null,
      });
    }
    return out;
  }, [scheme, t, value.userThemes]);

  const needle = query.trim().toLowerCase();
  const shown = cards.filter(
    (c) =>
      needle === "" ||
      c.label.toLowerCase().includes(needle) ||
      c.description.toLowerCase().includes(needle),
  );
  const selected = value.theme ?? null;

  // a typed query also asks the VS Code Marketplace (debounced)
  useEffect(() => {
    if (themes === null || needle.length < 2) {
      setItems([]);
      return;
    }
    const timer = window.setTimeout(() => {
      themes.searchMarketplace({ query: needle }).then(
        (answer) => setItems(answer.items),
        (error: unknown) =>
          setNote({ text: error instanceof Error ? error.message : String(error), error: true }),
      );
    }, 400);
    return () => window.clearTimeout(timer);
  }, [needle, themes]);

  const install = async (item: IndusMarketplaceItem) => {
    if (themes === null || busy !== null) return;
    setBusy(item.extension);
    try {
      const name = await installMarketplaceTheme(item, themes, appearance);
      setNote({ text: t("appearance.import.installed", { name }), error: false });
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setNote({ text: t("appearance.import.failed", { message }), error: true });
    } finally {
      setBusy(null);
    }
  };

  return (
    <PageBody>
      <SettingRow
        wide
        rowId="theme"
        highlighted={focusRow === "theme"}
        title={t("settingsA.theme.title")}
        description={t("settingsA.theme.body")}
        control={
          <Segmented
            label={t("settings.theme.title")}
            value={mode}
            options={MODES.map((m) => ({ value: m.value, label: t(m.key), icon: m.icon }))}
            onChange={setMode}
          />
        }
      />
      <label className="settingsA-search">
        <Search aria-hidden="true" size={12} />
        <input
          type="search"
          aria-label={t("settingsA.theme.search")}
          placeholder={t("settingsA.theme.search")}
          value={query}
          onChange={(event) => setQuery(event.currentTarget.value)}
        />
      </label>
      {shown.length === 0 && items.length === 0 && (
        <p className="settings-row-desc">{t("settingsA.theme.noMatch", { query })}</p>
      )}
      <div
        className="settingsA-theme-grid"
        role="radiogroup"
        aria-label={t("settingsA.theme.title")}
      >
        {shown.map((card) => {
          const isSelected = card.id === selected;
          return (
            <div
              key={card.key}
              className="settingsA-theme-card"
              data-selected={isSelected ? "true" : undefined}
            >
              {/* biome-ignore lint/a11y/useSemanticElements: a card that acts as a radio */}
              <button
                type="button"
                role="radio"
                aria-checked={isSelected}
                className="settingsA-theme-pick"
                onClick={() => appearance.choose(card.id)}
              >
                <ThemePreview swatches={card.swatches} />
                <span className="settingsA-theme-name">{card.label}</span>
                <span className="settingsA-theme-desc">{card.description}</span>
              </button>
              {card.removable !== null && (
                <button
                  type="button"
                  className="settingsA-icon settingsA-theme-remove"
                  aria-label={t("settingsA.theme.remove")}
                  title={t("settingsA.theme.remove")}
                  onClick={() => appearance.removeUserTheme(card.removable as string)}
                >
                  <Trash2 aria-hidden="true" size={14} />
                </button>
              )}
            </div>
          );
        })}
      </div>
      {items.length > 0 && (
        <ul className="settingsA-market" aria-label={t("appearance.import")}>
          {items.map((item) => (
            <li key={`${item.publisher}.${item.extension}`}>
              <span>
                <span className="settings-row-label">{item.displayName}</span>
                <span className="settings-row-desc">
                  {t("appearance.import.installs", { count: item.installs })}
                </span>
              </span>
              <OutlineButton disabled={busy !== null} onClick={() => void install(item)}>
                {t("appearance.import.install")}
              </OutlineButton>
            </li>
          ))}
        </ul>
      )}
      <StatusNote error={note?.error ?? false}>{note?.text ?? null}</StatusNote>
      <SettingRow
        title={t("appearance.accent")}
        control={
          <span className="settings-inline">
            <Slider
              label={t("appearance.accent")}
              value={value.accentHue ?? 220}
              max={359}
              format={(v) => `${v}°`}
              onChange={(hue) => appearance.setAccent(hue)}
            />
            <TextButton onClick={() => appearance.setAccent(null)}>
              {t("appearance.accent.reset")}
            </TextButton>
          </span>
        }
      />
      {profile !== null && (
        <SettingRow
          title={t("appearance.profile")}
          control={
            <CompactSelect
              label={t("appearance.profile")}
              value={value.profiles[profile]?.theme ?? ""}
              options={[
                { value: "", label: t("appearance.profile.same") },
                ...cards
                  .filter((c) => c.id !== null)
                  .map((c) => ({ value: c.id as string, label: c.label })),
              ]}
              onChange={(theme) => appearance.setProfile(profile, { theme })}
            />
          }
        />
      )}
      <SettingRow
        title={t("appearance.tuiSync")}
        control={
          <OutlineButton
            onClick={() =>
              void syncTui(api, profile, scheme).then((ok) => {
                if (ok) setNote({ text: t("appearance.tuiSynced"), error: false });
              })
            }
          >
            {t("appearance.tuiSync")}
          </OutlineButton>
        }
      />
    </PageBody>
  );
}

// -- Typography ----------------------------------------------------------------------------------------

const SCALES = [90, 100, 110, 125, 150, 175] as const;
/** The zoom bridge's levels are powers of 1.2 (Chromium's zoom factor). */
export function zoomLevelFor(percent: number): number {
  return Math.log(percent / 100) / Math.log(1.2);
}

function nearestScale(percent: number | null): string {
  if (percent === null) return "";
  const hit = SCALES.find((s) => Math.abs(s - percent) <= 1);
  return hit === undefined ? "" : String(hit);
}

function FontRow(props: {
  rowId: string;
  title: string;
  description: string;
  placeholder: string;
  clearLabel: string;
  previewLabel: string;
  sample: string;
  mono?: boolean;
  value: string | undefined;
  disabled: boolean;
  onChange: (value: string) => void;
}) {
  const t = useT();
  const { focusRow } = useSettings();
  const [draft, setDraft] = useState(props.value ?? "");
  useEffect(() => setDraft(props.value ?? ""), [props.value]);
  const commit = () => {
    if (draft.trim() !== (props.value ?? "")) props.onChange(draft.trim());
  };
  const family = (props.value ?? "") === "" ? undefined : props.value;
  return (
    <SettingRow
      wide
      rowId={props.rowId}
      highlighted={focusRow === props.rowId}
      title={props.title}
      description={props.disabled ? t("settings.notAvailable") : props.description}
      below={
        <div className="settingsA-font">
          <div className="settingsA-font-line">
            <input
              className="settings-input"
              aria-label={props.title}
              placeholder={props.placeholder}
              value={draft}
              disabled={props.disabled}
              spellCheck={false}
              onChange={(event) => setDraft(event.currentTarget.value)}
              onBlur={commit}
              onKeyDown={(event) => {
                if (event.key === "Enter") commit();
              }}
            />
            <TextButton
              disabled={props.disabled || (props.value ?? "") === ""}
              onClick={() => props.onChange("")}
            >
              {props.clearLabel}
            </TextButton>
          </div>
          <p className="settingsA-font-preview" data-mono={props.mono ? "true" : undefined}>
            <span>{props.previewLabel}</span>
            <span style={family === undefined ? undefined : { fontFamily: family }}>
              {props.sample}
            </span>
          </p>
        </div>
      }
    />
  );
}

export function AppearanceTypographyPage() {
  const t = useT();
  const { bridge, focusRow } = useSettings();
  const { prefs, error, update } = usePrefs(bridge);
  const [zoom, setZoom] = useState<number | null>(null);
  const [zoomError, setZoomError] = useState<string | null>(null);
  const zoomBridge = bridge?.zoom;
  const value = useAppearance(appearance);
  const profile = useSwitcher()?.getState().foreground ?? null;
  const own = profile === null ? undefined : value.profiles[profile];
  useEffect(() => {
    void attempt(zoomBridge ? () => zoomBridge.get() : null).then((answer) => {
      if (answer.ok) setZoom(answer.value.percent);
      else setZoomError(answer.code);
    });
    return zoomBridge?.onChanged((state) => setZoom(state.percent));
  }, [zoomBridge]);
  const options = SCALES.map((s) => ({ value: String(s), label: `${s}%` }));
  const chat = prefs?.chatTextScale ?? 110;
  return (
    <PageBody>
      <SettingRow
        rowId="ui-scale"
        highlighted={focusRow === "ui-scale"}
        title={t("settingsA.type.uiScale")}
        description={
          zoomError === null
            ? t("settingsA.type.uiScaleBody", {
                value: zoom === null ? "—" : `${Math.round(zoom)}%`,
              })
            : t("settings.notAvailable")
        }
        control={
          <fieldset className="settingsA-fieldset" disabled={zoomBridge === undefined}>
            <Segmented
              label={t("settingsA.type.uiScale")}
              value={nearestScale(zoom)}
              options={options}
              onChange={(next) => {
                if (zoomBridge === undefined) return;
                void attempt(() => zoomBridge.set({ level: zoomLevelFor(Number(next)) })).then(
                  (answer) => {
                    if (answer.ok) setZoom(answer.value.percent);
                  },
                );
              }}
            />
          </fieldset>
        }
      />
      <SettingRow
        rowId="chat-text"
        highlighted={focusRow === "chat-text"}
        title={t("settingsA.type.chatText")}
        description={
          error === null
            ? t("settingsA.type.chatTextBody", { value: `${chat}%` })
            : t("settings.notAvailable")
        }
        control={
          <fieldset className="settingsA-fieldset" disabled={prefs === null}>
            <Segmented
              label={t("settingsA.type.chatText")}
              value={String(chat)}
              options={options}
              onChange={(next) => void update({ chatTextScale: Number(next) })}
            />
          </fieldset>
        }
      />
      <FontRow
        rowId="chat-font"
        title={t("settingsA.type.chatFont")}
        description={t("settingsA.type.chatFontBody")}
        placeholder={t("settingsA.type.chatFontPlaceholder")}
        clearLabel={t("settingsA.type.useTheme")}
        previewLabel={t("settingsA.type.preview")}
        sample={t("settingsA.type.previewText")}
        value={own?.chatFont}
        disabled={profile === null}
        onChange={(chatFont) => profile !== null && appearance.setProfile(profile, { chatFont })}
      />
      <FontRow
        rowId="terminal-font"
        mono
        title={t("settingsA.type.terminalFont")}
        description={t("settingsA.type.terminalFontBody")}
        placeholder={t("settingsA.type.terminalFontPlaceholder")}
        clearLabel={t("settingsA.type.useDefault")}
        previewLabel={t("settingsA.type.glyphs")}
        sample="~/indus $ git status → 0 1 2 3 {} [] => != ~"
        value={own?.terminalFont}
        disabled={profile === null}
        onChange={(terminalFont) =>
          profile !== null && appearance.setProfile(profile, { terminalFont })
        }
      />
    </PageBody>
  );
}

// -- Window & layout -------------------------------------------------------------------------------------

/** Switched on but refused (no host, no tray): the tray state says why. */
function useTray() {
  const { bridge } = useSettings();
  const source = bridge?.ambient;
  const [tray, setTray] = useState<{
    enabled: boolean;
    available: boolean;
    reason: string | null;
  } | null>(null);
  useEffect(() => {
    let live = true;
    void attempt(source ? () => source.get() : null).then((answer) => {
      if (live && answer.ok) setTray(answer.value.tray);
    });
    const off = source?.onChanged((next) => setTray(next.tray));
    return () => {
      live = false;
      off?.();
    };
  }, [source]);
  return tray;
}

export function AppearanceWindowPage() {
  const t = useT();
  const { bridge, focusRow } = useSettings();
  const handle = usePrefs(bridge);
  const { prefs, error, update } = handle;
  const tray = useTray();
  const layout = useProvidedShellChrome()?.layout ?? null;
  const mode = useSyncExternalStore(
    (listener) => (layout === null ? () => undefined : layout.subscribe(listener)),
    () => layout?.mode() ?? null,
  );
  const trayMissing = tray?.enabled && !tray.available && tray.reason !== null;
  return (
    <PageBody>
      <SettingRow
        rowId="interface-mode"
        highlighted={focusRow === "interface-mode"}
        title={t("settingsA.window.mode")}
        description={layout === null ? t("settings.notAvailable") : t("settingsA.window.modeBody")}
        control={
          <fieldset className="settingsA-fieldset" disabled={layout === null}>
            <Segmented<InterfaceMode>
              label={t("settingsA.window.mode")}
              value={mode ?? "advanced"}
              options={[
                { value: "simple", label: t("settingsA.window.simple") },
                { value: "advanced", label: t("settingsA.window.advanced") },
              ]}
              onChange={(next) => layout?.setMode(next)}
            />
          </fieldset>
        }
      />
      <PrefChoiceRow
        handle={handle}
        rowId="session-density"
        field="sessionDensity"
        title={t("settingsA.window.density")}
        description={t("settingsA.window.densityBody")}
        options={[
          { value: "compact", label: t("settingsA.window.compact") },
          { value: "comfortable", label: t("settingsA.window.comfortable") },
          { value: "detailed", label: t("settingsA.window.detailed") },
        ]}
      />
      <PrefChoiceRow
        handle={handle}
        rowId="tab-strip"
        field="tabStrip"
        title={t("settingsA.window.tabs")}
        description={t("settingsA.window.tabsBody")}
        options={[
          { value: "auto", label: t("settingsA.window.auto") },
          { value: "always", label: t("settingsA.window.always") },
          { value: "never", label: t("settingsA.window.never") },
        ]}
      />
      <PrefChoiceRow
        handle={handle}
        rowId="app-actions"
        field="appActionsSide"
        title={t("settingsA.window.actions")}
        description={t("settingsA.window.actionsBody")}
        options={[
          { value: "right", label: t("settingsA.window.right") },
          { value: "left", label: t("settingsA.window.left") },
        ]}
      />
      <SettingRow
        rowId="tray"
        highlighted={focusRow === "tray"}
        title={t("settings.tray.title")}
        description={t("settings.tray.body")}
        control={
          <>
            <Switch
              label={t("settings.tray.title")}
              checked={prefs?.minimizeToTray ?? false}
              disabled={prefs === null}
              onChange={(checked) => void update({ minimizeToTray: checked })}
            />
            {trayMissing && (
              <StatusNote error>
                {t("settings.tray.unavailable", { reason: tray?.reason ?? "—" })}
              </StatusNote>
            )}
            {error !== null && <StatusNote error>{t("settings.notAvailable")}</StatusNote>}
          </>
        }
      />
      <PrefSwitchRow
        handle={handle}
        rowId="chat-backdrop"
        field="chatBackdrop"
        title={t("settingsA.window.backdrop")}
        description={t("settingsA.window.backdropBody")}
      />
      <PrefSwitchRow
        handle={handle}
        rowId="file-browser"
        field="fileBrowser"
        title={t("settingsA.window.files")}
        description={t("settingsA.window.filesBody")}
      />
      <PrefSwitchRow
        handle={handle}
        rowId="floating-composer"
        field="floatingComposer"
        title={t("settingsA.window.floating")}
        description={t("settingsA.window.floatingBody")}
      />
    </PageBody>
  );
}

// -- Chat display ----------------------------------------------------------------------------------------

/** Saved this long after the slider stops moving (one write per gesture, not per step). */
const BUBBLE_SAVE_MS = 250;

function BubbleRow(props: { handle: PrefsHandle }) {
  const t = useT();
  const { focusRow } = useSettings();
  const { prefs, error, update } = props.handle;
  const saved = prefs?.bubbleTransparency ?? DISPLAY_DEFAULTS.bubbleTransparency;
  const [draft, setDraft] = useState<number | null>(null);
  useEffect(() => {
    if (draft === null) return;
    const timer = window.setTimeout(() => {
      void update({ bubbleTransparency: draft }).then(() => setDraft(null));
    }, BUBBLE_SAVE_MS);
    return () => window.clearTimeout(timer);
  }, [draft, update]);
  return (
    <SettingRow
      rowId="message-bubble"
      highlighted={focusRow === "message-bubble"}
      title={t("settingsA.display.bubble")}
      description={
        prefs === null && error !== null
          ? t("settings.notAvailable")
          : t("settingsA.display.bubbleBody")
      }
      control={
        <Slider
          label={t("settingsA.display.bubble")}
          value={draft ?? saved}
          disabled={prefs === null}
          format={(v) => `${v}%`}
          onChange={setDraft}
        />
      }
    />
  );
}

export function AppearanceChatDisplayPage() {
  const t = useT();
  const { bridge } = useSettings();
  const handle = usePrefs(bridge);
  return (
    <PageBody>
      <BubbleRow handle={handle} />
      <PrefChoiceRow
        handle={handle}
        rowId="text-direction"
        field="textDirection"
        title={t("settingsA.display.direction")}
        description={t("settingsA.display.directionBody")}
        options={[
          { value: "auto", label: t("settingsA.window.auto") },
          { value: "rtl", label: t("settingsA.display.rtl") },
          { value: "ltr", label: t("settingsA.display.ltr") },
        ]}
      />
      <PrefSwitchRow
        handle={handle}
        rowId="timeline-bars"
        field="hideTimeline"
        title={t("settingsA.display.timeline")}
        description={t("settingsA.display.timelineBody")}
      />
      {/* reactions need the agent to react back; the hearts animation is not built yet */}
      <PlaceholderSwitchRow title={t("settingsA.display.reactions")} />
      <PlaceholderSwitchRow title={t("settingsA.display.hearts")} />
      <PrefChoiceRow
        handle={handle}
        rowId="tool-call-display"
        field="toolCallDisplay"
        title={t("settingsA.display.tools")}
        description={t("settingsA.display.toolsBody")}
        options={[
          { value: "product", label: t("settingsA.display.product") },
          { value: "technical", label: t("settingsA.display.technical") },
        ]}
      />
      {/* tool rows draw no code diff yet, so there is nothing to hide */}
      <PlaceholderSwitchRow title={t("settingsA.display.diffs")} />
      <PrefSwitchRow
        handle={handle}
        rowId="collapse-thinking"
        field="collapseThinking"
        title={t("settingsA.display.thinking")}
        description={t("settingsA.display.thinkingBody")}
      />
      {/* the transcript renders no third-party embeds (links stay links) */}
      <PlaceholderSegmentedRow
        title={t("settingsA.display.embeds")}
        value="off"
        options={[
          { value: "ask", label: t("settingsA.display.ask") },
          { value: "always", label: t("settingsA.window.always") },
          { value: "off", label: t("settingsA.display.off") },
        ]}
      />
    </PageBody>
  );
}

// -- Pet --------------------------------------------------------------------------------------------------

interface PetRow {
  readonly slug: string;
  readonly name: string;
  readonly shown: boolean;
}

interface PetList {
  readonly pets: readonly PetRow[];
  readonly active: string;
  readonly enabled: boolean;
}

/** `pet.*` are outside the generated method catalogue (see pet/PetOverlay.tsx). */
type LooseRequest = (method: string, params: object) => Promise<unknown>;

export function AppearancePetPage() {
  const t = useT();
  const { focusRow } = useSettings();
  const connection = useGatewayConnection();
  const request = useMemo(
    () =>
      connection === null ? null : (connection.request.bind(connection) as unknown as LooseRequest),
    [connection],
  );
  const [list, setList] = useState<PetList | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [tick, setTick] = useState(0);

  // biome-ignore lint/correctness/useExhaustiveDependencies: tick asks for a reload
  useEffect(() => {
    if (request === null) return;
    let live = true;
    request("pet.list", {}).then(
      (answer) => {
        if (live) setList(answer as PetList);
      },
      (failure: unknown) => {
        if (live) setError(failure instanceof Error ? failure.message : String(failure));
      },
    );
    return () => {
      live = false;
    };
  }, [request, tick]);

  const call = async (method: string, params: object) => {
    if (request === null) return;
    try {
      await request(method, params);
      setError(null);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
    }
    setTick((n) => n + 1);
  };

  const pets = list?.pets ?? [];
  const needle = query.trim().toLowerCase();
  const shown = pets.filter(
    (p) => needle === "" || p.name.toLowerCase().includes(needle) || p.slug.includes(needle),
  );
  const enabled = list?.enabled ?? false;
  return (
    <PageBody intro={t("settingsA.pet.intro")}>
      <SettingRow
        rowId="pet"
        highlighted={focusRow === "pet"}
        title={t("settingsA.pet.choose")}
        description={request === null ? t("settingsA.pet.offline") : t("settingsA.pet.chooseBody")}
        control={
          <Switch
            label={t("settingsA.pet.choose")}
            checked={enabled}
            disabled={list === null || pets.length === 0}
            onChange={(on) => {
              if (!on) void call("pet.disable", {});
              else {
                const slug = list?.active || pets[0]?.slug;
                if (slug !== undefined) void call("pet.select", { slug });
              }
            }}
          />
        }
      />
      {error !== null && <StatusNote error>{error}</StatusNote>}
      {list !== null && pets.length === 0 ? (
        <SettingsEmpty title={t("settingsA.pet.none")} body={t("settingsA.pet.noneBody")} />
      ) : (
        <>
          <label className="settingsA-search">
            <Search aria-hidden="true" size={12} />
            <input
              type="search"
              aria-label={t("settingsA.pet.search")}
              placeholder={t("settingsA.pet.search")}
              value={query}
              onChange={(event) => setQuery(event.currentTarget.value)}
            />
          </label>
          <div
            className="settingsA-pet-grid"
            role="radiogroup"
            aria-label={t("settingsA.pet.choose")}
          >
            {shown.map((pet) => {
              const chosen = list?.active === pet.slug;
              return (
                // biome-ignore lint/a11y/useSemanticElements: a tile that acts as a radio
                <button
                  key={pet.slug}
                  type="button"
                  role="radio"
                  aria-checked={chosen}
                  className="settingsA-pet"
                  data-off={enabled ? undefined : "true"}
                  onClick={() => void call("pet.select", { slug: pet.slug })}
                >
                  <span className="settingsA-pet-thumb">
                    <PawPrint aria-hidden="true" size={16} />
                  </span>
                  <span className="settingsA-pet-text">
                    <span>{pet.name}</span>
                    <span>{pet.slug}</span>
                  </span>
                </button>
              );
            })}
          </div>
          {list !== null && (
            <p className="settings-row-desc">
              {t("settingsA.pet.showing", { shown: shown.length, total: pets.length })}
            </p>
          )}
        </>
      )}
    </PageBody>
  );
}
