// Settings › Keyboard Shortcuts (W52-j): Key bindings (grouped in-app actions as key chips, search,
// reset all, per-row reset; the built-in keys as plain chips; composer, voice and profile actions
// since W59-K1-2) and Global shortcuts (quick entry and the HUD, with their live state).

import { AlertTriangle, RotateCcw, Search } from "lucide-react";
import { useState } from "react";
import type { IndusShortcutAction } from "../../global.d.ts";
import { type MessageKey, useT } from "../../i18n/index.tsx";
import { usePrefs, useSettings } from "../context.tsx";
import {
  conflictsFor,
  effectiveBindings,
  GLOBAL_ACTIONS,
  KEY_ACTIONS,
  KEY_GROUPS,
  type KeyGroup,
  keyGroupOf,
  normalizeAccelerator,
} from "../keybindings.ts";
import { PageBody, SettingRow, TextButton } from "../primitives.tsx";
import { KeyCapture, Note, notAvailable, shortcutStatus, useShortcutsView } from "./kit.tsx";

const GROUP_LABELS: Readonly<Record<KeyGroup, MessageKey>> = {
  general: "settings.keys2.groupGeneral",
  composer: "settings.keys2.groupComposer",
  tabs: "settings.keys2.groupTabs",
  conversation: "settings.keys2.groupConversation",
  profiles: "settings.keys2.groupProfiles",
};

type FixedKey = { keys: (mac: boolean) => string[]; label: MessageKey };

const mod = (mac: boolean) => (mac ? "⌘" : "Ctrl");
const shift = (mac: boolean) => (mac ? "⇧" : "Shift");

/** Built-in keys that work but are not rebindable, shown as plain chips in their group: the
 * composer's (the ones its `?` help card lists, W54-D2-9) and the pane shell's and window's
 * (panes/shortcuts.ts, the layout editor, the app menu; W59-K1-2). */
const FIXED_KEYS: Readonly<Partial<Record<KeyGroup, readonly FixedKey[]>>> = {
  general: [
    { keys: (mac) => [mod(mac), shift(mac), "N"], label: "settings.keys2.fixedNewWindow" },
    { keys: (mac) => [mod(mac), "J"], label: "settings.keys2.fixedRightSidebar" },
    { keys: (mac) => [mod(mac), shift(mac), "\\"], label: "settings.keys2.fixedLayout" },
  ],
  composer: [
    { keys: () => ["Enter"], label: "settings.keys2.fixedSend" },
    { keys: (mac) => [shift(mac), "Enter"], label: "settings.keys2.fixedNewline" },
    { keys: () => ["@"], label: "settings.keys2.fixedMention" },
    { keys: () => ["/"], label: "settings.keys2.fixedSlash" },
    { keys: () => ["?"], label: "settings.keys2.fixedHelp" },
    { keys: () => ["↑", "↓"], label: "settings.keys2.fixedHistory" },
    { keys: () => ["Esc"], label: "settings.keys2.fixedEscape" },
  ],
  tabs: [
    { keys: (mac) => [mod(mac), "T"], label: "settings.keys2.fixedNewTab" },
    { keys: (mac) => [mod(mac), "1–9"], label: "settings.keys2.fixedJumpTab" },
    { keys: (mac) => [mac ? "⌃" : "Ctrl", "PageUp"], label: "settings.keys2.fixedPrevTab" },
    {
      keys: (mac) => [mac ? "⌃" : "Ctrl", shift(mac), "Tab"],
      label: "settings.keys2.fixedPrevChat",
    },
  ],
};

const GLOBAL_TO_ACTION: Readonly<Record<string, IndusShortcutAction>> = {
  quickEntryShortcut: "quick-entry",
  hudSnapShortcut: "hud-snap",
  hudToggleShortcut: "hud-toggle",
};

/** Every binding (in-app and global) and a writer that checks validity and conflicts first. */
function useBindings() {
  const t = useT();
  const { bridge } = useSettings();
  const { prefs, error, update } = usePrefs(bridge);
  const [problem, setProblem] = useState<{ id: string; text: string } | null>(null);
  const bindings: Record<string, string> =
    prefs === null
      ? {}
      : {
          ...effectiveBindings(prefs.keybindings),
          quickEntryShortcut: prefs.quickEntryShortcut,
          hudSnapShortcut: prefs.hudSnapShortcut,
          hudToggleShortcut: prefs.hudToggleShortcut,
        };
  const labelOf = (id: string): string => {
    const action = KEY_ACTIONS.find((a) => a.id === id) ?? GLOBAL_ACTIONS.find((a) => a.id === id);
    return action === undefined ? id : t(action.label);
  };
  const commit = async (id: string, accelerator: string): Promise<boolean> => {
    if (prefs === null) return false;
    if (accelerator !== "" && normalizeAccelerator(accelerator) === null) {
      setProblem({ id, text: t("settings.key.invalid") });
      return false;
    }
    const clash = accelerator === "" ? [] : conflictsFor(accelerator, id, bindings);
    if (clash.length > 0) {
      setProblem({
        id,
        text: t("settings.key.conflict", {
          key: accelerator,
          action: clash.map(labelOf).join(", "),
        }),
      });
      return false;
    }
    setProblem(null);
    if (GLOBAL_ACTIONS.some((g) => g.id === id)) return update({ [id]: accelerator });
    const action = KEY_ACTIONS.find((a) => a.id === id);
    const overrides = { ...prefs.keybindings };
    if (action !== undefined && accelerator === action.defaultKey) delete overrides[id];
    else overrides[id] = accelerator;
    return update({ keybindings: overrides });
  };
  return { prefs, error, update, bindings, labelOf, commit, problem };
}

export function KeyBindingsPage() {
  const t = useT();
  const { bridge, focusRow } = useSettings();
  const keys = useBindings();
  const [needle, setNeedle] = useState("");
  const platform = bridge?.facts?.platform ?? "linux";
  if (keys.prefs === null) return <Note text={notAvailable(t, keys.error)} error />;
  const q = needle.trim().toLowerCase();
  const shown = KEY_ACTIONS.filter(
    (action) => q === "" || t(action.label).toLowerCase().includes(q),
  );
  return (
    <div
      className="settings-page-body"
      data-row="keybindings"
      data-highlighted={focusRow === "keybindings" ? "true" : undefined}
    >
      <div className="sb-intro-line">
        <p className="settings-intro">{t("settings.keyboard.body")}</p>
        <TextButton
          icon={<RotateCcw aria-hidden="true" size={12} />}
          onClick={() => void keys.update({ keybindings: {} })}
        >
          {t("settings.keys2.resetAll")}
        </TextButton>
      </div>
      {keys.problem !== null && (
        <p className="settings-row-error" role="alert">
          {keys.problem.text}
        </p>
      )}
      <label className="sb-search">
        <Search aria-hidden="true" size={13} />
        <input
          type="search"
          aria-label={t("settings.keys2.searchShortcuts")}
          placeholder={t("settings.keys2.searchShortcuts")}
          value={needle}
          onChange={(event) => setNeedle(event.currentTarget.value)}
        />
      </label>
      {KEY_GROUPS.map((group) => {
        const rows = shown.filter((action) => keyGroupOf(action.id) === group);
        const fixed = (FIXED_KEYS[group] ?? []).filter(
          (row) => q === "" || t(row.label).toLowerCase().includes(q),
        );
        if (rows.length === 0 && fixed.length === 0) return null;
        return (
          <section key={group} className="sb-key-group" aria-label={t(GROUP_LABELS[group])}>
            <h3 className="sb-caps">{t(GROUP_LABELS[group])}</h3>
            {rows.map((action) => {
              const value = keys.bindings[action.id] ?? "";
              const clash = keys.problem?.id === action.id ? keys.problem.text : null;
              return (
                <div key={action.id} className="sb-bind">
                  <span className="sb-bind-label">{t(action.label)}</span>
                  {clash !== null && (
                    <AlertTriangle aria-label={clash} size={14} className="sb-bind-warn" />
                  )}
                  <KeyCapture
                    label={t(action.label)}
                    platform={platform}
                    value={value}
                    onCommit={(accelerator) => void keys.commit(action.id, accelerator)}
                  />
                  <button
                    type="button"
                    className="sb-bind-reset"
                    aria-label={t("settings.reset")}
                    data-visible={value !== action.defaultKey ? "true" : undefined}
                    onClick={() => void keys.commit(action.id, action.defaultKey)}
                  >
                    <RotateCcw aria-hidden="true" size={12} />
                  </button>
                </div>
              );
            })}
            {fixed.map((row) => (
              <div key={row.label} className="sb-bind" data-fixed="true">
                <span className="sb-bind-label">{t(row.label)}</span>
                <span className="sb-chips" title={t("settings.keys2.fixed")}>
                  {row.keys(platform === "darwin").map((key) => (
                    <kbd key={key} className="sb-kbd">
                      {key}
                    </kbd>
                  ))}
                </span>
                <span className="sb-bind-reset" aria-hidden="true" />
              </div>
            ))}
          </section>
        );
      })}
    </div>
  );
}

export function GlobalShortcutsPage() {
  const t = useT();
  const { bridge, focusRow } = useSettings();
  const keys = useBindings();
  const shortcuts = useShortcutsView(bridge);
  const platform = bridge?.facts?.platform ?? "linux";
  if (keys.prefs === null) return <Note text={notAvailable(t, keys.error)} error />;
  const actionLabel = (action: IndusShortcutAction) => {
    const id = Object.keys(GLOBAL_TO_ACTION).find((k) => GLOBAL_TO_ACTION[k] === action);
    return id === undefined ? action : keys.labelOf(id);
  };
  return (
    <PageBody intro={t("settings.globalKeys.intro")}>
      <div
        data-row="global-shortcuts"
        data-highlighted={focusRow === "global-shortcuts" ? "true" : undefined}
      >
        {GLOBAL_ACTIONS.map((action) => {
          const status = shortcutStatus(
            t,
            shortcuts.view,
            GLOBAL_TO_ACTION[action.id] as IndusShortcutAction,
            actionLabel,
          );
          const clash = keys.problem?.id === action.id ? keys.problem.text : null;
          return (
            <SettingRow
              key={action.id}
              title={t(action.label)}
              description={
                status === null && clash === null ? undefined : (
                  <span
                    className="sb-status"
                    data-error={clash !== null || status?.error ? "true" : undefined}
                  >
                    {clash ?? status?.text}
                  </span>
                )
              }
              control={
                <KeyCapture
                  label={t(action.label)}
                  platform={platform}
                  value={keys.bindings[action.id] ?? ""}
                  onCommit={async (accelerator) => {
                    if (await keys.commit(action.id, accelerator)) shortcuts.reload();
                  }}
                />
              }
            />
          );
        })}
      </div>
    </PageBody>
  );
}
