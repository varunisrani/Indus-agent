// The table of hand-built settings pages (P20-32): which component renders each `custom` page.
// The pages live with their slice: settings A (W52-i) in `pagesA.tsx` / `modelPages.tsx` /
// `appearancePages.tsx`, settings B (W52-j) in `pagesB/`; only the persona page and the shared row
// stay here. Each reads what it shows from the backend (REST) or main (bridge) and writes back the
// same way; nothing that holds a credential is kept in the renderer after the call returns.

import { type ReactNode, useEffect, useId, useState } from "react";
import { useT } from "../i18n/index.tsx";
import { ConnectionsPage } from "./ConnectionsPage.tsx";
import { useSettings } from "./context.tsx";
import { LicencesPage } from "./LicencesPage.tsx";
import type { CustomPage } from "./pages.ts";
import { PAGES_A } from "./pagesA.tsx";
import { AboutPage, UninstallPage } from "./pagesB/about.tsx";
import { AdvancedDesktopPage, EnabledToolsetsRow, ProfilesPage } from "./pagesB/advanced.tsx";
import { RemoteUpdatesPage, ThisWindowPage } from "./pagesB/gateways.tsx";
import { GlobalShortcutsPage, KeyBindingsPage } from "./pagesB/keyboard.tsx";
import { ProviderKeysPage, ToolKeysPage } from "./pagesB/keys.tsx";
import { SecretInput } from "./pagesB/kit.tsx";
import { AlertsPage, SoundsPage } from "./pagesB/notifications.tsx";
import { PluginsSettingsPage } from "./pagesB/plugins.tsx";
import { AccountsPage, EndpointsPage, LocalModelsPage } from "./pagesB/providers.tsx";
import { ArchivedList, DefaultFolderPage } from "./pagesB/sessions.tsx";
import { WakeWordRows } from "./pagesB/voice.tsx";
import { PrimaryButton, SettingRow } from "./primitives.tsx";
import { rest } from "./settingsApi.ts";

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

// -- chat: personality ----------------------------------------------------------------------------

function PersonaPage() {
  const t = useT();
  const { api, profile, activeProfile, focusRow } = useSettings();
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
  // W54-D2-6: the persona is a document, not a field: label and line on top, then a full-width
  // editor with Save under it on the right (the profile editor's SOUL block, our styling).
  return (
    <div className="settings-rows">
      <SettingRow
        rowId="persona"
        highlighted={focusRow === "persona"}
        htmlFor={id}
        title={t("settings.persona.title")}
        description={t("settings.persona.body")}
        below={
          <div className="settings-persona">
            <textarea
              id={id}
              className="settings-input settings-textarea settings-persona-text"
              rows={16}
              spellCheck={false}
              value={text ?? ""}
              disabled={text === null}
              onChange={(event) => setText(event.currentTarget.value)}
            />
            <div className="settings-persona-actions">
              <Note text={note?.text ?? null} error={note?.error ?? false} />
              <PrimaryButton
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
              </PrimaryButton>
            </div>
          </div>
        }
      />
    </div>
  );
}

// -- the table --------------------------------------------------------------------------------------

/** Moved to `pagesB/kit.tsx` (W52-j); kept exported here for the onboarding flow. */
export { SecretInput };

const PAGES: Readonly<Partial<Record<CustomPage, () => ReactNode>>> = {
  ...PAGES_A, // settings slice A (W52-i): model, appearance, workspace, passwords
  persona: () => <PersonaPage />,
  // settings-b (W52-j) pages, in `pagesB/`
  "advanced-desktop": () => <AdvancedDesktopPage />,
  profiles: () => <ProfilesPage />,
  "voice-wake": () => <WakeWordRows />,
  notifications: () => <AlertsPage />,
  "notifications-sounds": () => <SoundsPage />,
  "providers-accounts": () => <AccountsPage />,
  "providers-keys": () => <ProviderKeysPage />,
  "providers-endpoints": () => <EndpointsPage />,
  "providers-local": () => <LocalModelsPage />,
  "gateways-connection": () => <ThisWindowPage />,
  "gateways-connections": () => <ConnectionsPage />, // P24-29
  "gateways-updates": () => <RemoteUpdatesPage />,
  keyboard: () => <KeyBindingsPage />,
  "keyboard-global": () => <GlobalShortcutsPage />,
  "tools-keys": () => <ToolKeysPage />,
  "advanced-toolsets": () => <EnabledToolsetsRow />,
  "sessions-archived-list": () => <ArchivedList />,
  "sessions-directory": () => <DefaultFolderPage />,
  plugins: () => <PluginsSettingsPage />, // P24-32: the desktop plugin inventory
  about: () => <AboutPage />,
  "about-uninstall": () => <UninstallPage />,
  "about-licences": () => <LicencesPage />, // W46 (F-2)
};

export function CustomSettingsPage(props: { page: CustomPage }) {
  return <>{PAGES[props.page]?.() ?? null}</>;
}
