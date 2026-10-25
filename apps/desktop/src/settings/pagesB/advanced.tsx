// Settings › Advanced (W52-j): Desktop & startup (keep awake, quick entry and its shortcut, run
// onboarding again) and Profiles (rename / delete through the window's switcher).
// W60-L2-2: Tool access starts with the enabled toolsets (the CLI selection new chats get).

import { useCallback, useEffect, useId, useState } from "react";
import type { IndusKeepAwakeMode } from "../../global.d.ts";
import { useT } from "../../i18n/index.tsx";
import { useSwitcher } from "../../profiles/switcherReact.tsx";
import { usePrefs, useSettings } from "../context.tsx";
import { normalizeAccelerator } from "../keybindings.ts";
import {
  OutlineButton,
  PageBody,
  Segmented,
  SettingRow,
  Switch,
  TextField,
} from "../primitives.tsx";
import { rest } from "../settingsApi.ts";
import { KeyCapture, Note, notAvailable, shortcutStatus, useShortcutsView } from "./kit.tsx";

export function AdvancedDesktopPage() {
  const t = useT();
  const { bridge, focusRow, openOnboarding } = useSettings();
  const { prefs, error, update } = usePrefs(bridge);
  const shortcuts = useShortcutsView(bridge);
  const [invalid, setInvalid] = useState(false);
  const platform = bridge?.facts?.platform ?? "linux";
  const quick = shortcuts.view?.shortcuts.find((s) => s.action === "quick-entry");
  const status = shortcutStatus(t, shortcuts.view, "quick-entry", (action) =>
    t(
      action === "hud-toggle"
        ? "settings.key.hudToggle"
        : action === "hud-snap"
          ? "settings.key.hudSnap"
          : "settings.key.quickEntry",
    ),
  );
  return (
    <PageBody>
      <SettingRow
        rowId="keep-awake"
        highlighted={focusRow === "keep-awake"}
        title={t("settings.keepAwake.title")}
        description={t("settings.keepAwake.body")}
        control={
          <>
            <Segmented<IndusKeepAwakeMode>
              label={t("settings.keepAwake.title")}
              value={prefs?.keepAwake ?? "while-working"}
              options={[
                { value: "never", label: t("settings.keepAwake.never") },
                { value: "while-working", label: t("settings.keepAwake.working") },
                { value: "always", label: t("settings.keepAwake.always") },
              ]}
              onChange={(value) => void update({ keepAwake: value })}
            />
            <Note text={notAvailable(t, error)} error />
          </>
        }
      />
      <SettingRow
        rowId="quick-entry-on"
        title={t("settings.quickEntryOn.title")}
        description={t("settings.quickEntryOn.body")}
        control={
          <>
            <Switch
              label={t("settings.quickEntryOn.title")}
              checked={quick?.enabled ?? true}
              disabled={shortcuts.view === null}
              onChange={(enabled) => void shortcuts.set({ action: "quick-entry", enabled })}
            />
            <Note text={notAvailable(t, shortcuts.error)} error />
          </>
        }
      />
      <SettingRow
        rowId="quick-entry"
        highlighted={focusRow === "quick-entry"}
        title={t("settings.quickEntry.title")}
        description={
          <>
            {t("settings.quickEntry.body")}
            {status !== null && (
              <span className="sb-status" data-error={status.error ? "true" : undefined}>
                {status.text}
              </span>
            )}
          </>
        }
        control={
          <>
            <KeyCapture
              label={t("settings.quickEntry.title")}
              platform={platform}
              value={prefs?.quickEntryShortcut ?? ""}
              onCommit={async (accelerator) => {
                if (accelerator !== "" && normalizeAccelerator(accelerator) === null) {
                  setInvalid(true);
                  return;
                }
                setInvalid(false);
                await update({ quickEntryShortcut: accelerator });
                shortcuts.reload();
              }}
            />
            {invalid && <Note text={t("settings.key.invalid")} error />}
          </>
        }
      />
      <SettingRow
        rowId="run-onboarding"
        highlighted={focusRow === "run-onboarding"}
        title={t("settings.onboardingAgain.title")}
        description={t("settings.onboardingAgain.body")}
        control={
          <OutlineButton onClick={openOnboarding}>
            {t("settings.onboardingAgain.run")}
          </OutlineButton>
        }
      />
    </PageBody>
  );
}

/**
 * Rename and delete through the window's switcher (P20-26): it leaves the profile and has main tear
 * down that profile's connection first, runs the backend change, then migrates or drops every
 * profile-keyed store (R08.7).
 */
export function ProfilesPage() {
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
    <PageBody intro={t("settings.profiles.body")}>
      <div data-row="profiles">
        {(names ?? []).map((name) =>
          name === "default" ? (
            <SettingRow key={name} title={name} description={t("settings.profiles.defaultFixed")} />
          ) : (
            <SettingRow
              key={name}
              title={name}
              control={
                <span className="settings-inline">
                  <TextField
                    aria-label={t("settings.profiles.newName", { name })}
                    placeholder={t("settings.profiles.newNameHint")}
                    value={drafts[name] ?? ""}
                    onChange={(event) => {
                      const value = event.currentTarget.value;
                      setDrafts((current) => ({ ...current, [name]: value }));
                    }}
                  />
                  <OutlineButton
                    disabled={switcher === null || (drafts[name] ?? "").trim() === ""}
                    onClick={() => void rename(name)}
                  >
                    {t("settings.profiles.rename")}
                  </OutlineButton>
                  <OutlineButton
                    aria-label={t("settings.profiles.delete", { name })}
                    disabled={switcher === null}
                    onClick={() => void remove(name)}
                  >
                    {t("settings.profiles.deleteShort")}
                  </OutlineButton>
                </span>
              }
            />
          ),
        )}
      </div>
      <Note text={error} error />
    </PageBody>
  );
}

// -- Tool access: enabled toolsets (W60-L2-2) --------------------------------------------------------

interface ToolsetSelection {
  readonly platform: string;
  readonly toolsets: readonly string[];
  readonly saved: boolean;
}

/** "web, file" → ["web", "file"]: comma- or line-separated, blanks dropped. */
export function toolsetNames(text: string): string[] {
  return text
    .split(/[,\n]/)
    .map((name) => name.trim())
    .filter((name) => name !== "");
}

/**
 * The toolsets new chats get (`platform_toolsets.cli`, else the `indus-cli` bundle), as one
 * comma-separated field. The backend checks every name (`PUT /api/tools/toolsets`); a refusal keeps
 * what is saved and says why.
 */
export function EnabledToolsetsRow() {
  const t = useT();
  const { api, profile, focusRow } = useSettings();
  const [selection, setSelection] = useState<ToolsetSelection | null>(null);
  const [draft, setDraft] = useState("");
  const [note, setNote] = useState<{ text: string; error: boolean } | null>(null);
  const fieldId = useId();
  const load = useCallback(() => {
    void rest<{ selection?: ToolsetSelection }>(api, "GET", "/api/tools/toolsets", {
      profile,
    }).then((answer) => {
      if (!answer.ok) setNote({ text: answer.message, error: true });
      else if (answer.data.selection !== undefined) {
        setSelection(answer.data.selection);
        setDraft(answer.data.selection.toolsets.join(", "));
      }
    });
  }, [api, profile]);
  useEffect(load, [load]);
  const commit = async () => {
    if (selection === null) return;
    const names = toolsetNames(draft);
    if (names.join(",") === selection.toolsets.join(",")) return;
    const answer = await rest<{ toolsets: string[] }>(api, "PUT", "/api/tools/toolsets", {
      body: { toolsets: names },
      profile,
    });
    if (answer.ok) {
      setSelection({ ...selection, toolsets: answer.data.toolsets, saved: true });
      setDraft(answer.data.toolsets.join(", "));
      setNote({ text: t("settings.saved"), error: false });
    } else setNote({ text: answer.message, error: true });
  };
  return (
    <SettingRow
      rowId="enabled-toolsets"
      highlighted={focusRow === "enabled-toolsets"}
      title={t("settings.toolsets.title")}
      description={
        selection !== null && !selection.saved
          ? t("settings.toolsets.bundle", { name: selection.toolsets.join(", ") })
          : t("settings.toolsets.body")
      }
      htmlFor={fieldId}
      control={
        <span className="sb-stack">
          <TextField
            id={fieldId}
            value={draft}
            disabled={selection === null}
            onChange={(event) => setDraft(event.currentTarget.value)}
            onBlur={() => void commit()}
            onKeyDown={(event) => {
              if (event.key === "Enter") void commit();
            }}
          />
          <Note text={note?.text ?? null} error={note?.error ?? false} />
        </span>
      }
    />
  );
}
