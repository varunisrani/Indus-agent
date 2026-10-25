// Small pieces the settings-b pages share (W52-j): status notes, key chips and the key-capture
// control, the global shortcuts' live state, and the update status hook.

import { Eye, EyeOff } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import type { IndusShortcutAction, IndusShortcutsView, IndusUpdateStatus } from "../../global.d.ts";
import { type MessageKey, type Translate, useT } from "../../i18n/index.tsx";
import { attempt, type SettingsBridge } from "../context.tsx";
import { acceleratorFromEvent, acceleratorKeys } from "../keybindings.ts";
import "./pagesB.css";

/** A 12px line under a control: quiet, or red for a failure. Nothing when empty. */
export function Note(props: { text: string | null; error?: boolean }) {
  if (props.text === null || props.text === "") return null;
  return (
    <p className={props.error ? "settings-row-error" : "settings-row-note"} role="status">
      {props.text}
    </p>
  );
}

/** A bridge refusal as words: "not in this build" or "could not be read". */
export function notAvailable(t: Translate, code: string | null): string | null {
  if (code === null) return null;
  return code === "not-available" ? t("settings.notAvailable") : t("settings.loadFailed");
}

/** One chip per key; an unbound action shows a single faint "set" chip. */
export function KeyChips(props: { accelerator: string; platform: string }) {
  const t = useT();
  const keys = acceleratorKeys(props.accelerator, props.platform);
  if (keys.length === 0) {
    return (
      <span className="sb-chips">
        <kbd className="sb-kbd" data-ghost="true">
          {t("settings.keys2.set")}
        </kbd>
      </span>
    );
  }
  return (
    <span className="sb-chips">
      {keys.map((key, index) => (
        // biome-ignore lint/suspicious/noArrayIndexKey: keys repeat only in invalid accelerators
        <kbd key={`${key}-${index}`} className="sb-kbd">
          {key}
        </kbd>
      ))}
    </span>
  );
}

/**
 * The chips as a button: a click starts capture ("Press keys…"), the next key press with a
 * modifier commits; Escape keeps the old keys, Backspace clears them.
 */
export function KeyCapture(props: {
  value: string;
  label: string;
  platform: string;
  onCommit: (accelerator: string) => void;
}) {
  const t = useT();
  const [recording, setRecording] = useState(false);
  return (
    <button
      type="button"
      className="sb-capture"
      aria-label={props.label}
      aria-pressed={recording}
      onClick={() => setRecording(true)}
      onBlur={() => setRecording(false)}
      onKeyDown={(event) => {
        if (!recording) return;
        event.preventDefault();
        event.stopPropagation();
        if (event.key === "Escape") {
          setRecording(false);
          return;
        }
        if (event.key === "Backspace" && !event.ctrlKey && !event.metaKey && !event.altKey) {
          setRecording(false);
          props.onCommit("");
          return;
        }
        const accelerator = acceleratorFromEvent(event);
        if (accelerator !== "") {
          setRecording(false);
          props.onCommit(accelerator);
        }
      }}
    >
      {recording ? (
        <span className="sb-chips">
          <kbd className="sb-kbd" data-recording="true">
            {t("settings.key.recording")}
          </kbd>
        </span>
      ) : (
        <KeyChips accelerator={props.value} platform={props.platform} />
      )}
    </button>
  );
}

/** Main's view of the three global shortcuts (registered or why not), kept current. */
export function useShortcutsView(bridge: SettingsBridge | null) {
  const source = bridge?.shortcuts;
  const [view, setView] = useState<IndusShortcutsView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const reload = useCallback(() => {
    void attempt(source ? () => source.get() : null).then((answer) => {
      if (answer.ok) setView(answer.value);
      else setError(answer.code);
    });
  }, [source]);
  useEffect(reload, [reload]);
  const set = async (payload: { action: IndusShortcutAction; enabled?: boolean }) => {
    const answer = await attempt(source ? () => source.set(payload) : null);
    if (answer.ok) setView(answer.value);
    else setError(answer.code);
  };
  return { view, error, reload, set };
}

const SHORTCUT_ERRORS: Readonly<Record<string, MessageKey>> = {
  taken: "settings.shortcut.taken",
  invalid: "settings.shortcut.invalid",
  failed: "settings.shortcut.failed",
};

/** "Shortcut is active." or why it is not, for one global action. */
export function shortcutStatus(
  t: Translate,
  view: IndusShortcutsView | null,
  action: IndusShortcutAction,
  labelOf: (action: IndusShortcutAction) => string,
): { text: string; error: boolean } | null {
  const entry = view?.shortcuts.find((s) => s.action === action);
  if (entry === undefined) return null;
  if (!entry.enabled) return { text: t("settings.shortcut.off"), error: false };
  if (entry.registered) return { text: t("settings.shortcut.active"), error: false };
  if (entry.error === "conflict" && entry.conflictWith !== undefined) {
    return {
      text: t("settings.shortcut.conflict", { action: labelOf(entry.conflictWith) }),
      error: true,
    };
  }
  const key = entry.error === null ? undefined : SHORTCUT_ERRORS[entry.error];
  return key === undefined ? null : { text: t(key), error: true };
}

/** The app's update status and its calls (About › Version & updates). */
export function useUpdates(bridge: SettingsBridge | null) {
  const source = bridge?.updates;
  const [status, setStatus] = useState<IndusUpdateStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    void attempt(source ? () => source.status() : null).then((answer) => {
      if (answer.ok) setStatus(answer.value);
      else setError(answer.code);
    });
    return source?.onProgress((next) => setStatus(next));
  }, [source]);
  const run = useCallback(async (call: (() => Promise<IndusUpdateStatus>) | null) => {
    const answer = await attempt(call);
    if (answer.ok) setStatus(answer.value);
    else setError(answer.code);
  }, []);
  return {
    status,
    error,
    check: () => run(source ? () => source.check() : null),
    setChannel: (channel: "stable" | "canary") =>
      run(source ? () => source.setChannel({ channel }) : null),
    apply: async () => {
      const answer = await attempt(source ? () => source.apply() : null);
      if (!answer.ok) setError(answer.code);
    },
  };
}

/** "3 min ago" style text for a past time (epoch ms or ISO; null when unreadable). */
export function relativeTime(
  t: Translate,
  when: number | string | null | undefined,
  now = Date.now(),
) {
  if (when === null || when === undefined) return null;
  const then = typeof when === "number" ? when : Date.parse(when);
  if (Number.isNaN(then)) return null;
  const minutes = Math.max(0, Math.round((now - then) / 60_000));
  if (minutes < 1) return t("settings.time.justNow");
  if (minutes < 60) return t("settings.time.minutes", { count: minutes });
  const hours = Math.round(minutes / 60);
  if (hours < 24) return t("settings.time.hours", { count: hours });
  return t("settings.time.days", { count: Math.round(hours / 24) });
}

/** A password field with a reveal toggle (shows only what the person typed). */
export function SecretInput(props: {
  id?: string;
  value: string;
  onChange: (value: string) => void;
  label: string;
  placeholder?: string;
}) {
  const t = useT();
  const [revealed, setRevealed] = useState(false);
  return (
    <span className="settings-inline">
      <input
        id={props.id}
        className="settings-input"
        type={revealed ? "text" : "password"}
        aria-label={props.label}
        value={props.value}
        placeholder={props.placeholder}
        autoComplete="off"
        spellCheck={false}
        onChange={(event) => props.onChange(event.currentTarget.value)}
      />
      <button
        type="button"
        className="icon-button"
        aria-label={t(revealed ? "settings.field.hide" : "settings.field.reveal")}
        onClick={() => setRevealed((now) => !now)}
      >
        {revealed ? <EyeOff aria-hidden="true" size={14} /> : <Eye aria-hidden="true" size={14} />}
      </button>
    </span>
  );
}
