// Settings slice A (W52-i): Workspace › Projects & discovery and Files & execution, and the two
// Passwords & Logins pages; plus the table of every hand-built page this slice owns, which
// `customPages.tsx` spreads into its own.

import { Plus } from "lucide-react";
import { type ReactNode, useEffect, useId, useState, useSyncExternalStore } from "react";
import { useProvidedShellChrome } from "../app/Shell.tsx";
import type { IndusFsLimits } from "../global.d.ts";
import { useT } from "../i18n/index.tsx";
import { profileKey } from "../profiles/storageFamilies.ts";
import { MAX_SCAN_DEPTH, type ProjectStore } from "../sidebar/projects.ts";
import {
  AppearanceChatDisplayPage,
  AppearanceGeneralPage,
  AppearancePetPage,
  AppearanceThemePage,
  AppearanceTypographyPage,
  AppearanceWindowPage,
} from "./appearancePages.tsx";
import { attempt, useSettings } from "./context.tsx";
import { SchemaFieldRow, SkeletonRows, StatusNote } from "./controlsA.tsx";
import {
  ModelAuxiliaryPage,
  ModelFallbacksPage,
  ModelMainPage,
  ModelMoaPage,
} from "./modelPages.tsx";
import type { CustomPage } from "./pages.ts";
import {
  OutlineButton,
  PageBody,
  Pill,
  SettingRow,
  SettingsEmpty,
  Switch,
  TextField,
} from "./primitives.tsx";
import { valueAt } from "./settingsApi.ts";

/** "a, b ,c" → ["a", "b", "c"] (one-line list fields, W52-i). */
export function commaList(text: string): string[] {
  return text
    .split(/[,\n]/)
    .map((part) => part.trim())
    .filter((part) => part !== "");
}

// -- Workspace › Projects & discovery ----------------------------------------------------------------

function useProjectPrefs(store: ProjectStore | null) {
  return useSyncExternalStore(
    (listener) => (store === null ? () => undefined : store.subscribe(listener)),
    () => (store === null ? null : store.getPrefs()),
  );
}

export function WorkspaceProjectsPage() {
  const t = useT();
  const { profile, activeProfile, focusRow } = useSettings();
  const chrome = useProvidedShellChrome();
  const store = chrome?.sidebar.projects(profileKey(profile ?? activeProfile)) ?? null;
  const prefs = useProjectPrefs(store);
  const dirId = useId();
  const rootsId = useId();
  const excludesId = useId();
  const depthId = useId();
  const off = store === null;
  const key = store?.profile ?? "";
  return (
    <PageBody>
      <SchemaFieldRow fieldKey="terminal.cwd" />
      <SettingRow
        rowId="default-directory"
        highlighted={focusRow === "default-directory"}
        htmlFor={dirId}
        title={t("settings.workspace.defaultDir")}
        description={t("settings.workspace.defaultDirBody")}
        control={
          <input
            id={dirId}
            className="settings-input"
            spellCheck={false}
            disabled={off}
            defaultValue={prefs?.defaultDirectory ?? ""}
            key={`dir-${key}`}
            onBlur={(event) => store?.setDefaultDirectory(event.currentTarget.value)}
          />
        }
      />
      <SettingRow
        rowId="repo-scan"
        highlighted={focusRow === "repo-scan"}
        title={t("settings.workspace.scan")}
        description={t("settings.workspace.scanBody")}
        control={
          <Switch
            label={t("settings.workspace.scan")}
            checked={prefs?.repoScan.enabled ?? false}
            disabled={off}
            onChange={(enabled) => store?.setRepoScan({ enabled })}
          />
        }
      />
      <SettingRow
        htmlFor={rootsId}
        title={t("settings.workspace.scanRoots")}
        control={
          <TextField
            id={rootsId}
            key={`roots-${key}`}
            disabled={off}
            placeholder={t("settingsA.list.placeholder")}
            defaultValue={(prefs?.repoScan.roots ?? []).join(", ")}
            onBlur={(event) => store?.setRepoScan({ roots: commaList(event.currentTarget.value) })}
          />
        }
      />
      <SettingRow
        htmlFor={excludesId}
        title={t("settings.workspace.scanExcludes")}
        control={
          <TextField
            id={excludesId}
            key={`excludes-${key}`}
            disabled={off}
            placeholder={t("settingsA.list.placeholder")}
            defaultValue={(prefs?.repoScan.excludes ?? []).join(", ")}
            onBlur={(event) =>
              store?.setRepoScan({ excludes: commaList(event.currentTarget.value) })
            }
          />
        }
      />
      <SettingRow
        htmlFor={depthId}
        title={t("settings.workspace.scanDepth")}
        control={
          <input
            id={depthId}
            type="number"
            className="settings-input"
            min={0}
            max={MAX_SCAN_DEPTH}
            disabled={off}
            value={prefs?.repoScan.maxDepth ?? 0}
            onChange={(event) => {
              const depth = Number(event.currentTarget.value);
              if (Number.isFinite(depth)) store?.setRepoScan({ maxDepth: depth });
            }}
          />
        }
      />
    </PageBody>
  );
}

// -- Workspace › Files & execution -------------------------------------------------------------------

export function WorkspaceFilesPage() {
  const t = useT();
  const { bridge, focusRow } = useSettings();
  const fs = bridge?.fs;
  const [limits, setLimits] = useState<IndusFsLimits | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    void attempt(fs ? () => fs.getLimits() : null).then((answer) => {
      if (answer.ok) setLimits(answer.value);
      else setError(answer.code);
    });
  }, [fs]);
  const limitId = useId();
  return (
    <PageBody>
      <SchemaFieldRow fieldKey="code_execution.mode" />
      <SchemaFieldRow fieldKey="file_read_max_chars" />
      <SettingRow
        rowId="data-url-limit"
        highlighted={focusRow === "data-url-limit"}
        htmlFor={limitId}
        title={t("settings.workspace.dataUrl")}
        description={t("settings.workspace.dataUrlBody")}
        control={
          <>
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
            {error !== null && (
              <StatusNote error>
                {error === "not-available" ? t("settings.notAvailable") : t("settings.loadFailed")}
              </StatusNote>
            )}
          </>
        }
      />
    </PageBody>
  );
}

// -- Passwords & Logins ---------------------------------------------------------------------------------

/** No vault API yet (the vault is CLI-only): the reference's shape with its empty state (W52-dsa-15). */
export function PasswordsCredentialsPage() {
  const t = useT();
  const { focusRow } = useSettings();
  return (
    <PageBody>
      <div
        className="settingsA-toolbar"
        data-row="saved-credentials"
        data-highlighted={focusRow === "saved-credentials" ? "true" : undefined}
      >
        <span title={t("settings.notAvailable")}>
          <OutlineButton disabled icon={<Plus aria-hidden="true" size={12} />}>
            {t("settingsA.vault.add")}
          </OutlineButton>
        </span>
      </div>
      <p className="settings-intro">{t("settingsA.vault.intro")}</p>
      <SettingsEmpty title={t("settingsA.vault.empty")} body={t("settingsA.vault.emptyBody")} />
    </PageBody>
  );
}

const SOURCES = ["local", "onepassword", "bitwarden"] as const;
const SOURCE_ORDER = ["onepassword", "bitwarden", "local"] as const;

/** Each manager's switch edits `browser.vault_sources` (W52-dsa-16; no detection API). */
export function PasswordsSourcesPage() {
  const t = useT();
  const { config, saveField, focusRow } = useSettings();
  const [note, setNote] = useState<string | null>(null);
  if (config === null) return <SkeletonRows count={3} />;
  const raw = valueAt(config, "browser.vault_sources");
  const list: string[] = Array.isArray(raw)
    ? raw.filter((v): v is string => typeof v === "string")
    : [...SOURCES];
  return (
    <PageBody intro={t("settingsA.sources.intro")}>
      <div
        data-row="password-managers"
        data-field="browser.vault_sources"
        data-highlighted={focusRow === "password-managers" ? "true" : undefined}
      >
        {SOURCE_ORDER.map((source) => {
          const on = list.includes(source);
          const title = t(`settingsA.sources.${source}`);
          return (
            <SettingRow
              key={source}
              rowId={`vault-${source}`}
              title={
                <span className="settingsA-title-pill">
                  {title}
                  <Pill tone={on ? "accent" : "neutral"}>
                    {on ? t("settingsA.sources.on") : t("settingsA.sources.off")}
                  </Pill>
                </span>
              }
              description={t(`settingsA.sources.${source}Body`)}
              control={
                <Switch
                  label={title}
                  checked={on}
                  disabled={saveField === undefined}
                  onChange={async (checked) => {
                    const next = checked
                      ? SOURCES.filter((s) => s === source || list.includes(s))
                      : list.filter((s) => s !== source);
                    setNote((await saveField?.("browser.vault_sources", next)) ?? null);
                  }}
                />
              }
            />
          );
        })}
      </div>
      <StatusNote error>{note}</StatusNote>
    </PageBody>
  );
}

/** The hand-built pages of slice A, by `CustomPage` id. */
export const PAGES_A: Readonly<Partial<Record<CustomPage, () => ReactNode>>> = {
  "model-main": () => <ModelMainPage />,
  "model-fallbacks": () => <ModelFallbacksPage />,
  "model-auxiliary": () => <ModelAuxiliaryPage />,
  "model-moa": () => <ModelMoaPage />,
  "appearance-general": () => <AppearanceGeneralPage />,
  "appearance-theme": () => <AppearanceThemePage />,
  "appearance-typography": () => <AppearanceTypographyPage />,
  "appearance-window": () => <AppearanceWindowPage />,
  "appearance-chat-display": () => <AppearanceChatDisplayPage />,
  "appearance-pet": () => <AppearancePetPage />,
  "workspace-projects": () => <WorkspaceProjectsPage />,
  "workspace-files": () => <WorkspaceFilesPage />,
  "passwords-credentials": () => <PasswordsCredentialsPage />,
  "passwords-sources": () => <PasswordsSourcesPage />,
};
