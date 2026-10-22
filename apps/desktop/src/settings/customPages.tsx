// The hand-built settings pages (P20-32): appearance, desktop-held preferences, providers,
// gateways, keyboard, notifications, archived sessions, persona and About. Each reads what it
// shows from the backend (REST) or main (bridge) and writes back the same way; nothing that holds
// a credential is kept in the renderer after the call returns.

import { Eye, EyeOff, RotateCcw } from "lucide-react";
import {
  type ReactNode,
  useCallback,
  useEffect,
  useId,
  useState,
  useSyncExternalStore,
} from "react";
import { LogsViewer } from "../app/LogsViewer.tsx";
import { useProvidedShellChrome } from "../app/Shell.tsx";
import { type ThemeMode, useTheme } from "../app/theme.tsx";
import { PluginsPage } from "../contrib/PluginsPage.tsx";
import type {
  IndusAmbientState,
  IndusFsLimits,
  IndusRemoteTestResult,
  IndusSecretsPolicyView,
  IndusUpdateStatus,
} from "../global.d.ts";
import { type MessageKey, useT } from "../i18n/index.tsx";
import { profileKey } from "../profiles/storageFamilies.ts";
import { useSwitcher } from "../profiles/switcherReact.tsx";
import { MAX_SCAN_DEPTH, type ProjectStore } from "../sidebar/projects.ts";
import { AppearancePresets } from "../theme/AppearancePage.tsx";
import { appWake } from "../wake/appWake.ts";
import { WakeToggle } from "../wake/WakeIndicator.tsx";
import { ConnectionsPage } from "./ConnectionsPage.tsx";
import { attempt, type SettingsBridge, usePrefs, useSettings } from "./context.tsx";
import {
  acceleratorFromEvent,
  conflictsFor,
  effectiveBindings,
  GLOBAL_ACTIONS,
  KEY_ACTIONS,
  normalizeAccelerator,
} from "./keybindings.ts";
import { LicencesPage } from "./LicencesPage.tsx";
import type { CustomPage } from "./pages.ts";
import { patchFor, rest, valueAt } from "./settingsApi.ts";
import { UninstallSection } from "./UninstallSection.tsx";

// -- small building blocks ----------------------------------------------------------------------

export function Row(props: {
  row?: string | undefined;
  label: string;
  description?: string | undefined;
  children: ReactNode;
  htmlFor?: string | undefined;
}) {
  const { focusRow } = useSettings();
  return (
    <div
      className="settings-row"
      data-row={props.row}
      data-highlighted={props.row !== undefined && focusRow === props.row ? "true" : undefined}
    >
      <div className="settings-row-text">
        {props.htmlFor === undefined ? (
          <span className="settings-row-label">{props.label}</span>
        ) : (
          <label className="settings-row-label" htmlFor={props.htmlFor}>
            {props.label}
          </label>
        )}
        {props.description !== undefined && (
          <p className="settings-row-desc">{props.description}</p>
        )}
      </div>
      <div className="settings-row-control">{props.children}</div>
    </div>
  );
}

function Note(props: { text: string | null; error?: boolean }) {
  if (props.text === null || props.text === "") return null;
  return (
    <p className={props.error ? "settings-row-error" : "settings-row-note"} role="status">
      {props.text}
    </p>
  );
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

function notAvailable(t: (key: MessageKey) => string, code: string | null): string | null {
  if (code === null) return null;
  return code === "not-available" ? t("settings.notAvailable") : t("settings.loadFailed");
}

// -- chat: personality ----------------------------------------------------------------------------

function PersonaPage() {
  const t = useT();
  const { api, profile, activeProfile } = useSettings();
  const name = profile ?? activeProfile ?? "default";
  const [text, setText] = useState<string | null>(null);
  const [note, setNote] = useState<{ text: string; error: boolean } | null>(null);
  const path = `/api/profiles/${encodeURIComponent(name)}/persona`;
  useEffect(() => {
    let live = true;
    void rest<{ content: string }>(api, "GET", path).then((answer) => {
      if (!live) return;
      if (answer.ok) setText(answer.data.content);
      else setNote({ text: answer.message, error: true });
    });
    return () => {
      live = false;
    };
  }, [api, path]);
  const id = useId();
  return (
    <div className="settings-rows">
      <Row
        row="persona"
        label={t("settings.persona.title")}
        description={t("settings.persona.body")}
        htmlFor={id}
      >
        <textarea
          id={id}
          className="settings-input settings-textarea settings-wide"
          rows={8}
          value={text ?? ""}
          disabled={text === null}
          onChange={(event) => setText(event.currentTarget.value)}
        />
        <button
          type="button"
          className="button button-primary"
          disabled={text === null}
          onClick={async () => {
            const answer = await rest(api, "PUT", path, { body: { content: text ?? "" } });
            setNote(
              answer.ok
                ? { text: t("settings.saved"), error: false }
                : { text: answer.message, error: true },
            );
          }}
        >
          {t("settings.save")}
        </button>
        <Note text={note?.text ?? null} error={note?.error ?? false} />
      </Row>
    </div>
  );
}

// -- appearance -----------------------------------------------------------------------------------

const THEME_CHOICES: ReadonlyArray<{ mode: ThemeMode; key: MessageKey }> = [
  { mode: "light", key: "theme.light" },
  { mode: "dark", key: "theme.dark" },
  { mode: "system", key: "theme.system" },
];

function ThemePage() {
  const t = useT();
  const { mode, setMode, resolved } = useTheme();
  const foreground = useSwitcher()?.getState().foreground ?? null;
  return (
    <div className="settings-rows">
      <Row row="theme" label={t("settings.theme.title")}>
        <div className="segmented" role="radiogroup" aria-label={t("settings.theme.title")}>
          {THEME_CHOICES.map((choice) => (
            // biome-ignore lint/a11y/useSemanticElements: a segmented control, not a native radio
            <button
              key={choice.mode}
              type="button"
              role="radio"
              aria-checked={mode === choice.mode}
              className="segmented-option settings-segment"
              onClick={() => setMode(choice.mode)}
            >
              {t(choice.key)}
            </button>
          ))}
        </div>
      </Row>
      {/* P24-33: presets, accent, per-profile theme and fonts, VS Code import */}
      <AppearancePresets profile={foreground} mode={resolved === "indus-dark" ? "dark" : "light"} />
    </div>
  );
}

const CHAT_SCALES = [90, 100, 110, 120, 130, 140] as const;

function TypographyPage() {
  const t = useT();
  const { bridge } = useSettings();
  const { prefs, error, update } = usePrefs(bridge);
  const [zoom, setZoom] = useState<number | null>(null);
  const [zoomError, setZoomError] = useState<string | null>(null);
  const zoomBridge = bridge?.zoom;
  useEffect(() => {
    void attempt(zoomBridge ? () => zoomBridge.get() : null).then((answer) => {
      if (answer.ok) setZoom(answer.value.percent);
      else setZoomError(answer.code);
    });
    return zoomBridge?.onChanged((state) => setZoom(state.percent));
  }, [zoomBridge]);
  const step = async (direction: "in" | "out" | "reset") => {
    const answer = await attempt(zoomBridge ? () => zoomBridge.step({ direction }) : null);
    if (answer.ok) setZoom(answer.value.percent);
  };
  const scaleId = useId();
  return (
    <div className="settings-rows">
      <Row
        row="ui-scale"
        label={t("settings.uiScale.title")}
        description={t("settings.uiScale.body")}
      >
        <span className="settings-inline">
          <button
            type="button"
            className="button button-secondary"
            onClick={() => void step("out")}
          >
            −
          </button>
          <output className="settings-value">
            {zoom === null ? "—" : `${Math.round(zoom)} %`}
          </output>
          <button type="button" className="button button-secondary" onClick={() => void step("in")}>
            +
          </button>
          <button
            type="button"
            className="icon-button"
            aria-label={t("settings.reset")}
            onClick={() => void step("reset")}
          >
            <RotateCcw aria-hidden="true" size={14} />
          </button>
        </span>
        <Note text={notAvailable(t, zoomError)} error />
      </Row>
      <Row
        row="chat-text"
        label={t("settings.chatText.title")}
        description={t("settings.chatText.body")}
        htmlFor={scaleId}
      >
        <select
          id={scaleId}
          className="settings-input"
          value={prefs?.chatTextScale ?? 110}
          disabled={prefs === null}
          onChange={(event) => void update({ chatTextScale: Number(event.currentTarget.value) })}
        >
          {CHAT_SCALES.map((scale) => (
            <option key={scale} value={scale}>{`${scale} %`}</option>
          ))}
        </select>
        <Note text={notAvailable(t, error)} error />
      </Row>
    </div>
  );
}

function Toggle(props: {
  checked: boolean;
  disabled?: boolean;
  label: string;
  onChange: (checked: boolean) => void;
}) {
  return (
    <input
      type="checkbox"
      role="switch"
      className="settings-switch"
      aria-label={props.label}
      checked={props.checked}
      aria-checked={props.checked}
      disabled={props.disabled}
      onChange={(event) => props.onChange(event.currentTarget.checked)}
    />
  );
}

/** The tray's live state from main (P20-36): whether this desktop can show one at all. */
function useAmbientState(bridge: SettingsBridge | null): IndusAmbientState | null {
  const source = bridge?.ambient;
  const [state, setState] = useState<IndusAmbientState | null>(null);
  useEffect(() => {
    let live = true;
    void attempt(source ? () => source.get() : null).then((answer) => {
      if (live && answer.ok) setState(answer.value);
    });
    const off = source?.onChanged((next) => setState(next));
    return () => {
      live = false;
      off?.();
    };
  }, [source]);
  return state;
}

/** Switched on but refused (no host, no tray): the tray state says why. */
function trayRefused(tray: IndusAmbientState["tray"] | null): boolean {
  if (tray === null) return false;
  return tray.enabled && !tray.available && tray.reason !== null;
}

function WindowPage() {
  const t = useT();
  const { bridge } = useSettings();
  const { prefs, error, update } = usePrefs(bridge);
  const ambient = useAmbientState(bridge);
  // Switched on but not working: no StatusNotifier host on Linux, or no tray on this desktop.
  // Windows are then never hidden (P20-36); the row says why instead of pretending.
  const tray = ambient?.tray ?? null;
  const trayMissing = trayRefused(tray);
  return (
    <div className="settings-rows">
      <Row row="tray" label={t("settings.tray.title")} description={t("settings.tray.body")}>
        <Toggle
          label={t("settings.tray.title")}
          checked={prefs?.minimizeToTray ?? false}
          disabled={prefs === null}
          onChange={(checked) => void update({ minimizeToTray: checked })}
        />
        {trayMissing && (
          <Note text={t("settings.tray.unavailable", { reason: tray?.reason ?? "—" })} error />
        )}
        <Note text={notAvailable(t, error)} error />
      </Row>
    </div>
  );
}

// -- advanced: desktop ------------------------------------------------------------------------------

/** Records one shortcut from the keyboard (Escape cancels, Backspace clears). */
export function AcceleratorInput(props: {
  value: string;
  label: string;
  onCommit: (accelerator: string) => void;
}) {
  const t = useT();
  const [recording, setRecording] = useState(false);
  return (
    <button
      type="button"
      className="button button-secondary settings-accelerator"
      aria-label={props.label}
      aria-pressed={recording}
      onClick={() => setRecording(true)}
      onBlur={() => setRecording(false)}
      onKeyDown={(event) => {
        if (!recording) return;
        event.preventDefault();
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
      {recording
        ? t("settings.key.recording")
        : props.value === ""
          ? t("settings.key.unbound")
          : props.value}
    </button>
  );
}

function AdvancedDesktopPage() {
  const t = useT();
  const { bridge } = useSettings();
  const { prefs, error, update } = usePrefs(bridge);
  const updates = useUpdates(bridge);
  const awakeId = useId();
  const channelId = useId();
  return (
    <div className="settings-rows">
      <Row row="update-channel" label={t("settings.updates.channel")} htmlFor={channelId}>
        <select
          id={channelId}
          className="settings-input"
          value={updates.status?.channel ?? "stable"}
          disabled={updates.status === null}
          onChange={(event) =>
            void updates.setChannel(event.currentTarget.value === "canary" ? "canary" : "stable")
          }
        >
          <option value="stable">{t("settings.updates.stable")}</option>
          <option value="canary">{t("settings.updates.canary")}</option>
        </select>
        <Note text={notAvailable(t, updates.error)} error />
      </Row>
      <Row
        row="keep-awake"
        label={t("settings.keepAwake.title")}
        description={t("settings.keepAwake.body")}
        htmlFor={awakeId}
      >
        <select
          id={awakeId}
          className="settings-input"
          value={prefs?.keepAwake ?? "while-working"}
          disabled={prefs === null}
          onChange={(event) => {
            const value = event.currentTarget.value;
            if (value === "never" || value === "while-working" || value === "always")
              void update({ keepAwake: value });
          }}
        >
          <option value="never">{t("settings.keepAwake.never")}</option>
          <option value="while-working">{t("settings.keepAwake.working")}</option>
          <option value="always">{t("settings.keepAwake.always")}</option>
        </select>
        <Note text={notAvailable(t, error)} error />
      </Row>
      <Row
        row="quick-entry"
        label={t("settings.quickEntry.title")}
        description={t("settings.quickEntry.body")}
      >
        <AcceleratorInput
          label={t("settings.quickEntry.title")}
          value={prefs?.quickEntryShortcut ?? ""}
          onCommit={(accelerator) => void update({ quickEntryShortcut: accelerator })}
        />
      </Row>
    </div>
  );
}

// -- updates and About ------------------------------------------------------------------------------

function useUpdates(bridge: SettingsBridge | null) {
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

const UPDATE_STATE_KEYS: Readonly<Record<IndusUpdateStatus["state"], MessageKey>> = {
  idle: "settings.updates.idle",
  checking: "settings.updates.checking",
  available: "settings.updates.available",
  downloading: "settings.updates.downloading",
  ready: "settings.updates.ready",
  installing: "settings.updates.installing",
  error: "settings.updates.error",
  manual: "settings.updates.manual",
};

function AboutPage() {
  const t = useT();
  const { bridge } = useSettings();
  const updates = useUpdates(bridge);
  const [backend, setBackend] = useState<string | null>(null);
  const connection = bridge?.connection;
  useEffect(() => {
    void attempt(connection ? () => connection.get() : null).then((answer) => {
      if (answer.ok) setBackend(answer.value.codeVersion);
    });
  }, [connection]);
  const status = updates.status;
  return (
    <div className="settings-rows">
      <Row row="version" label={t("settings.about.title")}>
        <dl className="settings-facts">
          <dt>{t("settings.about.app")}</dt>
          <dd>{bridge?.facts?.appVersion ?? "—"}</dd>
          <dt>{t("settings.about.backend")}</dt>
          <dd>{backend ?? "—"}</dd>
          <dt>{t("settings.updates.channel")}</dt>
          <dd>
            {status === null
              ? "—"
              : t(
                  status.channel === "canary"
                    ? "settings.updates.canary"
                    : "settings.updates.stable",
                )}
          </dd>
          <dt>{t("settings.about.state")}</dt>
          <dd>{status === null ? "—" : t(UPDATE_STATE_KEYS[status.state])}</dd>
        </dl>
      </Row>
      <Row label={t("settings.updates.title")}>
        <span className="settings-inline">
          <button
            type="button"
            className="button button-secondary"
            onClick={() => void updates.check()}
          >
            {t("settings.updates.check")}
          </button>
          {status?.state === "ready" || status?.state === "available" ? (
            <button
              type="button"
              className="button button-primary"
              onClick={() => void updates.apply()}
            >
              {t("settings.updates.apply")}
            </button>
          ) : null}
        </span>
        {status?.manualHint !== undefined && (
          <code className="settings-code">{status.manualHint}</code>
        )}
        {status?.error !== undefined && <Note text={status.error} error />}
        <Note text={notAvailable(t, updates.error)} error />
      </Row>
      <Row row="logs" label={t("settings.logs.title")}>
        <LogsViewer />
      </Row>
      <Row row="uninstall" label={t("settings.uninstall.title")}>
        <UninstallSection uninstall={bridge?.uninstall} />
      </Row>
    </div>
  );
}

// -- providers ------------------------------------------------------------------------------------

export interface ProviderRow {
  readonly name: string;
  readonly label: string;
  readonly description?: string;
  readonly family?: string;
  readonly auth_type: string;
  readonly configured: boolean;
  readonly device_login: boolean;
}

export interface ModelOptions {
  readonly providers: readonly ProviderRow[];
  readonly current: { readonly provider: string | null; readonly model: string | null };
}

function AccountsPage() {
  const t = useT();
  const { api, profile, openOnboarding } = useSettings();
  const [rows, setRows] = useState<readonly ProviderRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    void rest<ModelOptions>(api, "GET", "/api/model/options", { profile }).then((answer) => {
      if (answer.ok) setRows(answer.data.providers);
      else setError(answer.message);
    });
  }, [api, profile]);
  const device = (rows ?? []).filter((row) => row.device_login);
  return (
    <div className="settings-rows">
      <Row
        row="device-sign-in"
        label={t("settings.accounts.title")}
        description={t("settings.accounts.body")}
      >
        <button type="button" className="button button-primary" onClick={openOnboarding}>
          {t("settings.accounts.setup")}
        </button>
      </Row>
      {device.length === 0 && rows !== null && (
        <p className="settings-empty">{t("settings.accounts.none")}</p>
      )}
      {device.map((row) => (
        <Row key={row.name} label={row.label} description={row.description}>
          <span className="settings-badge" data-on={row.configured ? "true" : "false"}>
            {t(row.configured ? "settings.accounts.signedIn" : "settings.accounts.signedOut")}
          </span>
        </Row>
      ))}
      <Note text={error} error />
    </div>
  );
}

interface EnvRow {
  readonly category: string;
  readonly description: string;
  readonly advanced: boolean;
  readonly is_password: boolean;
  readonly is_set: boolean;
  readonly redacted_value: string | null;
}

function KeyRow(props: {
  name: string;
  row: EnvRow;
  onSave: (name: string, value: string) => Promise<string | null>;
  onClear: (name: string) => Promise<string | null>;
}) {
  const t = useT();
  const [value, setValue] = useState("");
  const [note, setNote] = useState<{ text: string; error: boolean } | null>(null);
  const id = useId();
  return (
    <Row label={props.name} description={props.row.description} htmlFor={id}>
      <span className="settings-inline">
        {props.row.is_set && <code className="settings-code">{props.row.redacted_value}</code>}
        <SecretInput
          id={id}
          label={props.name}
          value={value}
          onChange={setValue}
          placeholder={t(props.row.is_set ? "settings.keys.replace" : "settings.keys.paste")}
        />
        <button
          type="button"
          className="button button-primary"
          disabled={value.trim() === ""}
          onClick={async () => {
            const failure = await props.onSave(props.name, value);
            if (failure === null) setValue(""); // the key leaves the renderer
            setNote(
              failure === null
                ? { text: t("settings.saved"), error: false }
                : { text: failure, error: true },
            );
          }}
        >
          {t("settings.save")}
        </button>
        {props.row.is_set && (
          <button
            type="button"
            className="button button-secondary"
            onClick={async () => {
              const failure = await props.onClear(props.name);
              setNote(
                failure === null
                  ? { text: t("settings.keys.cleared"), error: false }
                  : { text: failure, error: true },
              );
            }}
          >
            {t("settings.keys.clear")}
          </button>
        )}
      </span>
      <Note text={note?.text ?? null} error={note?.error ?? false} />
    </Row>
  );
}

function KeysPage() {
  const t = useT();
  const { api, profile } = useSettings();
  const [rows, setRows] = useState<Readonly<Record<string, EnvRow>> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [newName, setNewName] = useState("");
  const [newValue, setNewValue] = useState("");
  const load = useCallback(() => {
    void rest<Record<string, EnvRow>>(api, "GET", "/api/env", { profile }).then((answer) => {
      if (answer.ok) setRows(answer.data);
      else setError(answer.message);
    });
  }, [api, profile]);
  useEffect(load, [load]);
  const save = async (name: string, value: string) => {
    const answer = await rest(api, "PUT", "/api/env", { body: { key: name, value }, profile });
    if (answer.ok) load();
    return answer.ok ? null : answer.message;
  };
  const clear = async (name: string) => {
    const answer = await rest(api, "DELETE", "/api/env", { body: { key: name }, profile });
    if (answer.ok) load();
    return answer.ok ? null : answer.message;
  };
  const entries = Object.entries(rows ?? {}).filter(
    ([, row]) => row.is_password && (row.category === "provider" || row.category === "custom"),
  );
  const nameId = useId();
  return (
    <div className="settings-rows">
      <Row row="api-keys" label={t("settings.keys.title")} description={t("settings.keys.body")}>
        <span />
      </Row>
      {entries.map(([name, row]) => (
        <KeyRow key={name} name={name} row={row} onSave={save} onClear={clear} />
      ))}
      <Row
        label={t("settings.keys.other")}
        description={t("settings.keys.otherBody")}
        htmlFor={nameId}
      >
        <span className="settings-inline">
          <input
            id={nameId}
            className="settings-input"
            placeholder="EXAMPLE_API_KEY"
            value={newName}
            spellCheck={false}
            onChange={(event) => setNewName(event.currentTarget.value.toUpperCase())}
          />
          <SecretInput label={t("settings.keys.value")} value={newValue} onChange={setNewValue} />
          <button
            type="button"
            className="button button-primary"
            disabled={!/^[A-Z][A-Z0-9_]*$/.test(newName) || newValue.trim() === ""}
            onClick={async () => {
              const failure = await save(newName, newValue);
              if (failure === null) {
                setNewName("");
                setNewValue("");
              } else setError(failure);
            }}
          >
            {t("settings.save")}
          </button>
        </span>
      </Row>
      <Note text={error} error />
    </div>
  );
}

export interface EndpointTestResult {
  readonly ok: boolean;
  readonly status: number | null;
  readonly models: readonly string[];
  readonly message: string;
}

/** `POST /api/model/test-endpoint`: the endpoint's own `/models`, asked from the backend. */
export async function testEndpoint(
  api: Parameters<typeof rest>[0],
  body: { base_url: string; api_key?: string; key_env?: string },
  profile: string | null,
): Promise<EndpointTestResult> {
  const answer = await rest<EndpointTestResult>(api, "POST", "/api/model/test-endpoint", {
    body,
    profile,
  });
  if (answer.ok) return answer.data;
  return { ok: false, status: answer.status, models: [], message: answer.message };
}

/** `MY-LLM` → `MY_LLM_API_KEY` (the variable a custom endpoint's key is stored under). */
export function keyEnvFor(name: string): string {
  const base = name
    .toUpperCase()
    .replace(/[^A-Z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
  return `${base === "" ? "CUSTOM" : base}_API_KEY`;
}

interface CustomEntry {
  readonly base_url?: string;
  readonly key_env?: string;
}

function EndpointsPage() {
  const t = useT();
  const { api, profile, config, reloadConfig } = useSettings();
  const providers = (valueAt(config, "providers") ?? {}) as Readonly<Record<string, CustomEntry>>;
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [key, setKey] = useState("");
  const [result, setResult] = useState<EndpointTestResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ text: string; error: boolean } | null>(null);
  const [rowResults, setRowResults] = useState<Readonly<Record<string, EndpointTestResult>>>({});
  const nameId = useId();
  const valid = /^[a-z0-9][a-z0-9._-]*$/i.test(name) && /^https?:\/\//i.test(url.trim());
  return (
    <div className="settings-rows">
      <Row
        row="custom-endpoints"
        label={t("settings.endpoints.title")}
        description={t("settings.endpoints.body")}
      >
        <span />
      </Row>
      {Object.entries(providers)
        .filter(([, entry]) => typeof entry?.base_url === "string" && entry.base_url !== "")
        .map(([entryName, entry]) => (
          <Row key={entryName} label={entryName} description={entry.base_url}>
            <button
              type="button"
              className="button button-secondary"
              onClick={async () => {
                const tested = await testEndpoint(
                  api,
                  {
                    base_url: entry.base_url ?? "",
                    ...(entry.key_env ? { key_env: entry.key_env } : {}),
                  },
                  profile,
                );
                setRowResults((now) => ({ ...now, [entryName]: tested }));
              }}
            >
              {t("settings.endpoints.test")}
            </button>
            <Note
              text={rowResults[entryName]?.message ?? null}
              error={rowResults[entryName]?.ok === false}
            />
          </Row>
        ))}
      <Row label={t("settings.endpoints.add")} htmlFor={nameId}>
        <div className="settings-form">
          <input
            id={nameId}
            className="settings-input"
            aria-label={t("settings.endpoints.name")}
            placeholder={t("settings.endpoints.name")}
            value={name}
            onChange={(event) => setName(event.currentTarget.value)}
          />
          <input
            className="settings-input"
            aria-label={t("settings.endpoints.url")}
            placeholder="http://127.0.0.1:8000/v1"
            value={url}
            spellCheck={false}
            onChange={(event) => setUrl(event.currentTarget.value)}
          />
          <SecretInput
            label={t("settings.endpoints.key")}
            placeholder={t("settings.endpoints.keyOptional")}
            value={key}
            onChange={setKey}
          />
          <span className="settings-inline">
            <button
              type="button"
              className="button button-secondary"
              disabled={!/^https?:\/\//i.test(url.trim()) || busy}
              onClick={async () => {
                setBusy(true);
                setResult(
                  await testEndpoint(
                    api,
                    { base_url: url.trim(), ...(key.trim() === "" ? {} : { api_key: key.trim() }) },
                    profile,
                  ),
                );
                setBusy(false);
              }}
            >
              {t("settings.endpoints.test")}
            </button>
            <button
              type="button"
              className="button button-primary"
              disabled={!valid || busy}
              onClick={async () => {
                setBusy(true);
                const keyEnv = keyEnvFor(name);
                const entry: Record<string, string> = { base_url: url.trim() };
                if (key.trim() !== "") entry.key_env = keyEnv;
                const saved = await rest(api, "PUT", "/api/config", {
                  body: { config: patchFor(`providers.${name}`, entry) },
                  profile,
                });
                let failure = saved.ok ? null : saved.message;
                if (failure === null && key.trim() !== "") {
                  const stored = await rest(api, "PUT", "/api/env", {
                    body: { key: keyEnv, value: key.trim() },
                    profile,
                  });
                  failure = stored.ok ? null : stored.message;
                }
                setBusy(false);
                if (failure === null) {
                  setKey("");
                  setName("");
                  setUrl("");
                  setResult(null);
                  reloadConfig();
                }
                setNote(
                  failure === null
                    ? { text: t("settings.saved"), error: false }
                    : { text: failure, error: true },
                );
              }}
            >
              {t("settings.save")}
            </button>
          </span>
          {result !== null && <Note text={result.message} error={!result.ok} />}
          <Note text={note?.text ?? null} error={note?.error ?? false} />
        </div>
      </Row>
    </div>
  );
}

// -- gateways --------------------------------------------------------------------------------------

const LEG_KEYS: Readonly<Record<IndusRemoteTestResult["http"], MessageKey>> = {
  ok: "settings.remote.leg.ok",
  unauthorized: "settings.remote.leg.unauthorized",
  unreachable: "settings.remote.leg.unreachable",
  error: "settings.remote.leg.error",
};

function ConnectionPage() {
  const t = useT();
  const { bridge } = useSettings();
  const connection = bridge?.connection;
  const switcher = useSwitcher();
  /** A saved or forgotten remote changes the connection: a soft re-home (P20-26, R08.6). */
  const rehome = async <T,>(change: () => Promise<T>): Promise<T> => {
    if (switcher === null) return change();
    let result: { value: T } | null = null;
    const outcome = await switcher.applyConnectionChange(async () => {
      result = { value: await change() };
    });
    if (result === null) throw new Error(outcome.ok ? "no answer" : outcome.error);
    return (result as { value: T }).value;
  };
  const [mode, setMode] = useState<string | null>(null);
  const [saved, setSaved] = useState<{ url: string | null; hasToken: boolean } | null>(null);
  const [url, setUrl] = useState("");
  const [token, setToken] = useState("");
  const [result, setResult] = useState<IndusRemoteTestResult | null>(null);
  const [note, setNote] = useState<{ text: string; error: boolean } | null>(null);
  const load = useCallback(() => {
    void attempt(connection ? () => connection.get() : null).then((answer) => {
      if (answer.ok) setMode(answer.value.mode);
    });
    void attempt(connection ? () => connection.getRemote() : null).then((answer) => {
      if (answer.ok) {
        setSaved(answer.value);
        setUrl(answer.value.url ?? "");
      } else setNote({ text: notAvailable(t, answer.code) ?? "", error: true });
    });
  }, [connection, t]);
  useEffect(load, [load]);
  const legText = (leg: IndusRemoteTestResult["http"]) => t(LEG_KEYS[leg]);
  const urlId = useId();
  return (
    <div className="settings-rows">
      <Row row="connection-mode" label={t("settings.gateway.mode")}>
        <span className="settings-badge" data-on="true">
          {mode === null
            ? "—"
            : t(mode === "remote" ? "settings.gateway.remote" : "settings.gateway.local")}
        </span>
      </Row>
      <Row
        label={t("settings.remote.title")}
        description={t("settings.remote.body")}
        htmlFor={urlId}
      >
        <div className="settings-form">
          <input
            id={urlId}
            className="settings-input"
            placeholder="https://indus.example.com"
            value={url}
            spellCheck={false}
            onChange={(event) => setUrl(event.currentTarget.value)}
          />
          <SecretInput
            label={t("settings.remote.token")}
            placeholder={t(
              saved?.hasToken ? "settings.remote.tokenStored" : "settings.remote.token",
            )}
            value={token}
            onChange={setToken}
          />
          <span className="settings-inline">
            <button
              type="button"
              className="button button-secondary"
              disabled={url.trim() === "" || token === ""}
              onClick={async () => {
                const answer = await attempt(
                  connection ? () => connection.testRemote({ url: url.trim(), token }) : null,
                );
                if (answer.ok) setResult(answer.value);
                else setNote({ text: answer.message, error: true });
              }}
            >
              {t("settings.remote.test")}
            </button>
            <button
              type="button"
              className="button button-primary"
              disabled={url.trim() === "" || token === ""}
              onClick={async () => {
                const answer = await attempt(
                  connection
                    ? () => rehome(() => connection.saveRemote({ url: url.trim(), token }))
                    : null,
                );
                if (answer.ok) {
                  setResult(answer.value);
                  if (answer.value.saved) {
                    setToken(""); // the token leaves the renderer
                    setNote({ text: t("settings.remote.saved"), error: false });
                    load();
                  } else
                    setNote({
                      text: answer.value.message ?? t("settings.remote.notSaved"),
                      error: true,
                    });
                } else setNote({ text: answer.message, error: true });
              }}
            >
              {t("settings.save")}
            </button>
            {saved?.url !== null && saved !== null && (
              <button
                type="button"
                className="button button-secondary"
                onClick={async () => {
                  const answer = await attempt(
                    connection ? () => rehome(() => connection.forgetRemote()) : null,
                  );
                  if (answer.ok) {
                    setNote({ text: t("settings.remote.forgotten"), error: false });
                    setUrl("");
                    load();
                  } else setNote({ text: answer.message, error: true });
                }}
              >
                {t("settings.remote.forget")}
              </button>
            )}
          </span>
          {result !== null && (
            <p className="settings-row-note" role="status">
              {t("settings.remote.legs", { http: legText(result.http), ws: legText(result.ws) })}
            </p>
          )}
          <Note text={note?.text ?? null} error={note?.error ?? false} />
        </div>
      </Row>
    </div>
  );
}

function StoragePage() {
  const t = useT();
  const { bridge } = useSettings();
  const secrets = bridge?.secrets;
  const [policy, setPolicy] = useState<IndusSecretsPolicyView | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    void attempt(secrets ? () => secrets.getPolicy() : null).then((answer) => {
      if (answer.ok) setPolicy(answer.value);
      else setError(answer.code);
    });
  }, [secrets]);
  return (
    <div className="settings-rows">
      <Row
        row="secure-storage"
        label={t("settings.storage.title")}
        description={t("settings.storage.body")}
      >
        <Toggle
          label={t("settings.storage.title")}
          checked={policy?.store === "keychain"}
          disabled={policy === null || (!policy.keychainUsable && policy.store === "file")}
          onChange={async (checked) => {
            const answer = await attempt(
              secrets ? () => secrets.setPolicy({ store: checked ? "keychain" : "file" }) : null,
            );
            if (answer.ok) setPolicy(answer.value);
            else setError(answer.code);
          }}
        />
        {policy !== null && !policy.keychainUsable && (
          <Note text={t("settings.storage.unusable", { reason: policy.reason })} error />
        )}
        <Note text={notAvailable(t, error)} error />
      </Row>
    </div>
  );
}

// -- keyboard --------------------------------------------------------------------------------------

function KeyboardPage() {
  const t = useT();
  const { bridge } = useSettings();
  const { prefs, error, update } = usePrefs(bridge);
  const [conflict, setConflict] = useState<string | null>(null);
  if (prefs === null) return <Note text={notAvailable(t, error)} error />;
  const bindings: Record<string, string> = {
    ...effectiveBindings(prefs.keybindings),
    quickEntryShortcut: prefs.quickEntryShortcut,
    hudSnapShortcut: prefs.hudSnapShortcut,
    hudToggleShortcut: prefs.hudToggleShortcut,
  };
  const labelOf = (id: string): string => {
    const action = KEY_ACTIONS.find((a) => a.id === id) ?? GLOBAL_ACTIONS.find((a) => a.id === id);
    return action === undefined ? id : t(action.label);
  };
  const commit = async (id: string, accelerator: string) => {
    if (accelerator !== "" && normalizeAccelerator(accelerator) === null) {
      setConflict(t("settings.key.invalid"));
      return;
    }
    const clash = accelerator === "" ? [] : conflictsFor(accelerator, id, bindings);
    if (clash.length > 0) {
      setConflict(
        t("settings.key.conflict", { key: accelerator, action: clash.map(labelOf).join(", ") }),
      );
      return;
    }
    setConflict(null);
    if (GLOBAL_ACTIONS.some((g) => g.id === id)) {
      await update({ [id]: accelerator });
      return;
    }
    const action = KEY_ACTIONS.find((a) => a.id === id);
    const overrides = { ...prefs.keybindings };
    if (action !== undefined && accelerator === action.defaultKey) delete overrides[id];
    else overrides[id] = accelerator;
    await update({ keybindings: overrides });
  };
  return (
    <div className="settings-rows" data-row="keybindings">
      <p className="settings-row-desc">{t("settings.keyboard.body")}</p>
      {conflict !== null && (
        <p className="settings-row-error" role="alert">
          {conflict}
        </p>
      )}
      {[...KEY_ACTIONS, ...GLOBAL_ACTIONS].map((action) => (
        <Row key={action.id} label={t(action.label)}>
          <span className="settings-inline">
            <AcceleratorInput
              label={t(action.label)}
              value={bindings[action.id] ?? ""}
              onCommit={(accelerator) => void commit(action.id, accelerator)}
            />
            {"defaultKey" in action && bindings[action.id] !== action.defaultKey && (
              <button
                type="button"
                className="icon-button"
                aria-label={t("settings.reset")}
                onClick={() => void commit(action.id, action.defaultKey)}
              >
                <RotateCcw aria-hidden="true" size={14} />
              </button>
            )}
          </span>
        </Row>
      ))}
    </div>
  );
}

// -- notifications ---------------------------------------------------------------------------------

function NotificationsPage() {
  const t = useT();
  const { bridge } = useSettings();
  const { prefs, error, update } = usePrefs(bridge);
  const rows = [
    { id: "turnComplete", row: "notify-turn", label: "settings.notify.turnComplete" },
    { id: "approvals", row: "notify-approvals", label: "settings.notify.approvals" },
    { id: "sounds", row: "notify-sounds", label: "settings.notify.sounds" },
  ] as const;
  return (
    <div className="settings-rows">
      {rows.map((row) => (
        <Row key={row.id} row={row.row} label={t(row.label)}>
          <Toggle
            label={t(row.label)}
            checked={prefs?.notifications[row.id] ?? true}
            disabled={prefs === null}
            onChange={(checked) => void update({ notifications: { [row.id]: checked } })}
          />
        </Row>
      ))}
      <Note text={notAvailable(t, error)} error />
    </div>
  );
}

// -- sessions ----------------------------------------------------------------------------------------

interface SessionRow {
  readonly id?: string;
  readonly session_id?: string;
  readonly title?: string | null;
}

function ArchivedPage() {
  const t = useT();
  const { api, profile } = useSettings();
  const [rows, setRows] = useState<readonly SessionRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(() => {
    void rest<{ sessions: SessionRow[] }>(api, "GET", "/api/sessions?archived=only&limit=100", {
      profile,
    }).then((answer) => {
      if (answer.ok) setRows(answer.data.sessions);
      else setError(answer.message);
    });
  }, [api, profile]);
  useEffect(load, [load]);
  return (
    <div className="settings-rows" data-row="archived-sessions">
      {rows !== null && rows.length === 0 && (
        <p className="settings-empty">{t("settings.archived.none")}</p>
      )}
      {(rows ?? []).map((row) => {
        const id = row.id ?? row.session_id ?? "";
        return (
          <Row key={id} label={row.title || id}>
            <button
              type="button"
              className="button button-secondary"
              onClick={async () => {
                const answer = await rest(api, "PATCH", `/api/sessions/${encodeURIComponent(id)}`, {
                  body: { archived: false },
                  profile,
                });
                if (answer.ok) load();
                else setError(answer.message);
              }}
            >
              {t("settings.archived.restore")}
            </button>
          </Row>
        );
      })}
      <Note text={error} error />
    </div>
  );
}

// -- workspace: on this computer -------------------------------------------------------------------

function useProjectPrefs(store: ProjectStore | null) {
  return useSyncExternalStore(
    (listener) => (store === null ? () => undefined : store.subscribe(listener)),
    () => (store === null ? null : store.getPrefs()),
  );
}

const lines = (text: string) =>
  text
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line !== "");

/** Default folder, repo scan (the sidebar's per-profile records, P20-25) and the read cap (P20-29). */
function WorkspaceDesktopPage() {
  const t = useT();
  const { bridge, profile, activeProfile } = useSettings();
  const chrome = useProvidedShellChrome();
  const store = chrome?.sidebar.projects(profileKey(profile ?? activeProfile)) ?? null;
  const prefs = useProjectPrefs(store);
  const fs = bridge?.fs;
  const [limits, setLimits] = useState<IndusFsLimits | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    void attempt(fs ? () => fs.getLimits() : null).then((answer) => {
      if (answer.ok) setLimits(answer.value);
      else setError(answer.code);
    });
  }, [fs]);
  const dirId = useId();
  const rootsId = useId();
  const excludesId = useId();
  const depthId = useId();
  const limitId = useId();
  return (
    <div className="settings-rows">
      <Row
        row="default-directory"
        label={t("settings.workspace.defaultDir")}
        description={t("settings.workspace.defaultDirBody")}
        htmlFor={dirId}
      >
        <input
          id={dirId}
          className="settings-input"
          spellCheck={false}
          disabled={store === null}
          defaultValue={prefs?.defaultDirectory ?? ""}
          key={`dir-${store?.profile ?? ""}`}
          onBlur={(event) => store?.setDefaultDirectory(event.currentTarget.value)}
        />
      </Row>
      <Row
        row="repo-scan"
        label={t("settings.workspace.scan")}
        description={t("settings.workspace.scanBody")}
      >
        <Toggle
          label={t("settings.workspace.scan")}
          checked={prefs?.repoScan.enabled ?? false}
          disabled={store === null}
          onChange={(checked) => store?.setRepoScan({ enabled: checked })}
        />
        <div className="settings-form" key={`scan-${store?.profile ?? ""}`}>
          <label htmlFor={rootsId}>{t("settings.workspace.scanRoots")}</label>
          <textarea
            id={rootsId}
            className="settings-input"
            spellCheck={false}
            disabled={store === null}
            defaultValue={(prefs?.repoScan.roots ?? []).join("\n")}
            onBlur={(event) => store?.setRepoScan({ roots: lines(event.currentTarget.value) })}
          />
          <label htmlFor={excludesId}>{t("settings.workspace.scanExcludes")}</label>
          <textarea
            id={excludesId}
            className="settings-input"
            spellCheck={false}
            disabled={store === null}
            defaultValue={(prefs?.repoScan.excludes ?? []).join("\n")}
            onBlur={(event) => store?.setRepoScan({ excludes: lines(event.currentTarget.value) })}
          />
          <label htmlFor={depthId}>{t("settings.workspace.scanDepth")}</label>
          <input
            id={depthId}
            type="number"
            className="settings-input"
            min={0}
            max={MAX_SCAN_DEPTH}
            disabled={store === null}
            value={prefs?.repoScan.maxDepth ?? 0}
            onChange={(event) => {
              const depth = Number(event.currentTarget.value);
              if (Number.isFinite(depth)) store?.setRepoScan({ maxDepth: depth });
            }}
          />
        </div>
      </Row>
      <Row
        row="data-url-limit"
        label={t("settings.workspace.dataUrl")}
        description={t("settings.workspace.dataUrlBody")}
        htmlFor={limitId}
      >
        <input
          id={limitId}
          type="number"
          className="settings-input"
          disabled={limits === null}
          min={limits?.dataUrlMinMb ?? 1}
          max={limits?.dataUrlCeilingMb ?? 4096}
          value={limits?.dataUrlMaxMb ?? ""}
          onChange={async (event) => {
            const value = Math.trunc(Number(event.currentTarget.value));
            if (!Number.isFinite(value) || fs === undefined) return;
            const answer = await attempt(() => fs.setLimits({ dataUrlMaxMb: value }));
            if (answer.ok) setLimits(answer.value);
            else setError(answer.code);
          }}
        />
        <Note text={notAvailable(t, error)} error />
      </Row>
    </div>
  );
}

// -- advanced: profiles ----------------------------------------------------------------------------

/**
 * Rename and delete through the window's switcher (P20-26): it leaves the profile and has main tear
 * down that profile's connection first, runs the backend change, then migrates or drops every
 * profile-keyed store (R08.7).
 */
function ProfilesPage() {
  const t = useT();
  const { api } = useSettings();
  const switcher = useSwitcher();
  const [names, setNames] = useState<readonly string[] | null>(null);
  const [drafts, setDrafts] = useState<Readonly<Record<string, string>>>({});
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(() => {
    void rest<{ profiles: Array<{ name: string }> }>(api, "GET", "/api/profiles").then((answer) => {
      if (answer.ok) setNames(answer.data.profiles.map((row) => row.name));
      else setError(answer.message);
    });
  }, [api]);
  useEffect(load, [load]);
  const run = async (change: () => Promise<{ ok: boolean; message: string }>) => {
    const answer = await change();
    if (!answer.ok) throw new Error(answer.message);
  };
  const rename = async (from: string) => {
    const to = (drafts[from] ?? "").trim();
    if (to === "" || to === from || switcher === null) return;
    const result = await switcher.renameProfile(from, to, () =>
      run(async () => {
        const answer = await rest(api, "PATCH", `/api/profiles/${encodeURIComponent(from)}`, {
          body: { new_name: to },
        });
        return { ok: answer.ok, message: answer.ok ? "" : answer.message };
      }),
    );
    if (result.ok) {
      setError(null);
      load();
    } else setError(result.error);
  };
  const remove = async (name: string) => {
    if (switcher === null) return;
    if (!window.confirm(t("settings.profiles.confirmDelete", { name }))) return;
    const result = await switcher.deleteProfile(name, () =>
      run(async () => {
        const answer = await rest(api, "DELETE", `/api/profiles/${encodeURIComponent(name)}`);
        return { ok: answer.ok, message: answer.ok ? "" : answer.message };
      }),
    );
    if (result.ok) {
      setError(null);
      load();
    } else setError(result.error);
  };
  return (
    <div className="settings-rows" data-row="profiles">
      <p className="settings-row-note">{t("settings.profiles.body")}</p>
      {(names ?? []).map((name) =>
        name === "default" ? (
          <Row key={name} label={name}>
            <span className="settings-row-note">{t("settings.profiles.defaultFixed")}</span>
          </Row>
        ) : (
          <Row key={name} label={name}>
            <span className="settings-inline">
              <input
                className="settings-input"
                aria-label={t("settings.profiles.newName", { name })}
                spellCheck={false}
                value={drafts[name] ?? ""}
                onChange={(event) => {
                  const value = event.currentTarget.value;
                  setDrafts((current) => ({ ...current, [name]: value }));
                }}
              />
              <button
                type="button"
                className="button button-secondary"
                disabled={switcher === null || (drafts[name] ?? "").trim() === ""}
                onClick={() => void rename(name)}
              >
                {t("settings.profiles.rename")}
              </button>
              <button
                type="button"
                className="button button-secondary"
                disabled={switcher === null}
                onClick={() => void remove(name)}
              >
                {t("settings.profiles.delete", { name })}
              </button>
            </span>
          </Row>
        ),
      )}
      <Note text={error} error />
    </div>
  );
}

// -- the table --------------------------------------------------------------------------------------

/** Voice (P24-20, P24-35): the wake word's switch; dictation and read-aloud live in the composer. */
function VoicePage() {
  const t = useT();
  const controller = appWake();
  return (
    <div className="flex flex-col gap-3">
      {controller === null ? (
        <p className="text-sm text-muted">{t("settings.voice.noWindow")}</p>
      ) : (
        <WakeToggle controller={controller} />
      )}
    </div>
  );
}

const PAGES: Readonly<Record<CustomPage, () => ReactNode>> = {
  persona: () => <PersonaPage />,
  "chat-voice": () => <VoicePage />,
  "appearance-theme": () => <ThemePage />,
  "appearance-typography": () => <TypographyPage />,
  "appearance-window": () => <WindowPage />,
  "advanced-desktop": () => <AdvancedDesktopPage />,
  "providers-accounts": () => <AccountsPage />,
  "providers-keys": () => <KeysPage />,
  "providers-endpoints": () => <EndpointsPage />,
  "gateways-connection": () => <ConnectionPage />,
  "gateways-connections": () => <ConnectionsPage />, // P24-29
  "gateways-storage": () => <StoragePage />,
  keyboard: () => <KeyboardPage />,
  notifications: () => <NotificationsPage />,
  "sessions-archived": () => <ArchivedPage />,
  "workspace-desktop": () => <WorkspaceDesktopPage />,
  profiles: () => <ProfilesPage />,
  plugins: () => <PluginsPage />, // P24-32: the desktop plugin inventory
  about: () => <AboutPage />,
  "about-licences": () => <LicencesPage />, // W46 (F-2)
};

export function CustomSettingsPage(props: { page: CustomPage }) {
  return <>{PAGES[props.page]()}</>;
}
