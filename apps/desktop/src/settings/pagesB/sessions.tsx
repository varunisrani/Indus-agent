// Settings › Sessions (W52-j): the archived list under Archive & retention's schema rows, and the
// Default project folder (the sidebar's per-profile default directory, P20-25).

import { Archive, FolderOpen } from "lucide-react";
import { useCallback, useEffect, useState, useSyncExternalStore } from "react";
import { useProvidedShellChrome } from "../../app/Shell.tsx";
import { useT } from "../../i18n/index.tsx";
import { profileKey } from "../../profiles/storageFamilies.ts";
import type { ProjectStore } from "../../sidebar/projects.ts";
import { useSettings } from "../context.tsx";
import {
  OutlineButton,
  PageBody,
  SectionHeading,
  SettingRow,
  SettingsEmpty,
  TextButton,
} from "../primitives.tsx";
import { rest, valueAt } from "../settingsApi.ts";
import { Note } from "./kit.tsx";

interface SessionRow {
  readonly id?: string;
  readonly session_id?: string;
  readonly title?: string | null;
}

export function ArchivedList() {
  const t = useT();
  const { api, profile, focusRow } = useSettings();
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
    <div
      data-row="archived-sessions"
      data-highlighted={focusRow === "archived-sessions" ? "true" : undefined}
    >
      <SectionHeading icon={<Archive aria-hidden="true" />} title={t("settings.archived.title")} />
      <p className="settings-intro">{t("settings.archived2.intro")}</p>
      {rows !== null && rows.length === 0 && (
        <SettingsEmpty
          title={t("settings.archived2.none")}
          body={t("settings.archived2.noneBody")}
        />
      )}
      {(rows ?? []).map((row) => {
        const id = row.id ?? row.session_id ?? "";
        return (
          <SettingRow
            key={id}
            title={row.title || id}
            control={
              <OutlineButton
                onClick={async () => {
                  const answer = await rest(
                    api,
                    "PATCH",
                    `/api/sessions/${encodeURIComponent(id)}`,
                    { body: { archived: false }, profile },
                  );
                  if (answer.ok) load();
                  else setError(answer.message);
                }}
              >
                {t("settings.archived.restore")}
              </OutlineButton>
            }
          />
        );
      })}
      <Note text={error} error />
    </div>
  );
}

function useProjectPrefs(store: ProjectStore | null) {
  return useSyncExternalStore(
    (listener) => (store === null ? () => undefined : store.subscribe(listener)),
    () => (store === null ? null : store.getPrefs()),
  );
}

export function DefaultFolderPage() {
  const t = useT();
  const { profile, activeProfile, config, focusRow } = useSettings();
  const chrome = useProvidedShellChrome();
  const store = chrome?.sidebar.projects(profileKey(profile ?? activeProfile)) ?? null;
  const prefs = useProjectPrefs(store);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const current = prefs?.defaultDirectory ?? "";
  const cwd = valueAt(config, "terminal.cwd");
  const fallback = typeof cwd === "string" && cwd !== "" ? cwd : ".";
  return (
    <PageBody intro={t("settings.sessionDir.intro")}>
      <SettingRow
        rowId="sessions-directory"
        highlighted={focusRow === "sessions-directory"}
        title={
          editing ? (
            <input
              className="settings-input sb-full"
              aria-label={t("settings.sessionDir.title")}
              // biome-ignore lint/a11y/noAutofocus: the field opens on a click
              autoFocus
              spellCheck={false}
              value={draft}
              onChange={(event) => setDraft(event.currentTarget.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  store?.setDefaultDirectory(draft.trim());
                  setEditing(false);
                }
                if (event.key === "Escape") {
                  event.stopPropagation();
                  setEditing(false);
                }
              }}
            />
          ) : (
            <span className={current === "" ? "sb-faint" : "sb-mono-title"}>
              {current === "" ? t("settings.field.notSet") : current}
            </span>
          )
        }
        description={t("settings.sessionDir.body", { folder: fallback })}
        control={
          <span className="settings-inline">
            <TextButton
              icon={<FolderOpen aria-hidden="true" size={13} />}
              disabled={store === null}
              onClick={() => {
                setDraft(current);
                setEditing(true);
              }}
            >
              {t("settings.sessionDir.change")}
            </TextButton>
            {current !== "" && (
              <TextButton onClick={() => store?.setDefaultDirectory("")}>
                {t("settings.sessionDir.clear")}
              </TextButton>
            )}
          </span>
        }
      />
      {store === null && <Note text={t("settings.sessionDir.noWindow")} />}
    </PageBody>
  );
}
