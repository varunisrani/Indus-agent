// Settings › Advanced › Plugins (P24-32 wiring, D-16.799): the window's desktop plugin inventory.
//
// One card per plugin file main listed: its name (or id), kind (a disk file or the desktop half of
// an agent plugin package), file, status (loaded, off, error, refused by main's load-time check)
// and the reason when it did not load. A plugin that passed the check has an on/off switch: the
// choice is applied at once (`PluginLoader.setEnabled`: off unloads it and every contribution it
// made; on loads it again) and kept for the next start. A package half stays off while its agent
// plugin is off, whatever the switch says. "Check the folder again" asks main to rescan. Under a
// loaded plugin's card come its own settings pages (`plugin-settings-page` contributions), each in
// its own error boundary.

import "../app/manage/manage.css";
import { type ReactNode, useEffect, useState } from "react";
import { desktopBridge } from "../app/bridge.ts";
import type { IndusPluginFile } from "../global.d.ts";
import { type MessageKey, useT } from "../i18n/index.tsx";
import { onPluginLoaderStarted, runningPluginLoader } from "./host.ts";
import type { PluginLoader, PluginRecord, PluginStatus } from "./loader.ts";
import { ContributionBoundary, useContributions } from "./react.tsx";

const STATUS_KEYS: Readonly<Record<PluginStatus, MessageKey>> = {
  loaded: "plugins.status.loaded",
  disabled: "plugins.status.disabled",
  error: "plugins.status.error",
  refused: "plugins.status.refused",
  loading: "plugins.status.loading",
};

const TONES: Readonly<Record<PluginStatus, string>> = {
  loaded: "ok",
  disabled: "muted",
  error: "error",
  refused: "error",
  loading: "muted",
};

/** The running loader, following its start (the page may open before plugins started). */
function useLoader(given: PluginLoader | null | undefined): PluginLoader | null {
  const [loader, setLoader] = useState<PluginLoader | null>(() =>
    given === undefined ? runningPluginLoader() : given,
  );
  useEffect(() => {
    if (given !== undefined) return;
    return onPluginLoaderStarted(() => setLoader(runningPluginLoader()));
  }, [given]);
  return given === undefined ? loader : given;
}

function useInventory(loader: PluginLoader | null): PluginRecord[] {
  const [records, setRecords] = useState<PluginRecord[]>(() => loader?.inventory() ?? []);
  useEffect(() => {
    if (loader === null) {
      setRecords([]);
      return;
    }
    setRecords(loader.inventory());
    return loader.subscribe(() => setRecords(loader.inventory()));
  }, [loader]);
  return records;
}

function Rendered(props: { render: () => ReactNode }) {
  return <>{props.render()}</>;
}

/** A loaded plugin's own settings pages. */
function PluginSettings(props: { id: string }) {
  const pages = useContributions("plugin-settings-page").filter(
    (c) => c.source === `plugin:${props.id}`,
  );
  if (pages.length === 0) return null;
  return (
    <div className="manage-body" data-plugin-settings={props.id}>
      {pages.map((c) => (
        <section key={c.id} aria-label={String(c.title)}>
          <h4>{String(c.title)}</h4>
          <ContributionBoundary id={c.id}>
            <Rendered render={c.render as () => ReactNode} />
          </ContributionBoundary>
        </section>
      ))}
    </div>
  );
}

export interface PluginsPageProps {
  /** The loader (tests); the window's running loader when left out. */
  loader?: PluginLoader | null;
  /** Ask main to look at the folders again (tests); the bridge's `plugins.rescan` otherwise. */
  rescan?: () => Promise<{ files: IndusPluginFile[] }>;
}

export function PluginsPage(props: PluginsPageProps) {
  const t = useT();
  const loader = useLoader(props.loader);
  const records = useInventory(loader);
  const [note, setNote] = useState("");
  const rescan =
    props.rescan ??
    (() => {
      const plugins = desktopBridge()?.plugins;
      return plugins === undefined
        ? Promise.reject(new Error(t("plugins.unavailable")))
        : plugins.rescan();
    });
  if (loader === null) return <p className="settings-row-note">{t("plugins.unavailable")}</p>;
  const toggle = async (record: PluginRecord, on: boolean) => {
    try {
      await loader.setEnabled(record.id, on);
      setNote("");
    } catch (error) {
      setNote(error instanceof Error ? error.message : t("manage.failed"));
    }
  };
  const again = async () => {
    try {
      const { files } = await rescan();
      await loader.sync(files);
      setNote("");
    } catch (error) {
      setNote(error instanceof Error ? error.message : t("manage.failed"));
    }
  };
  return (
    <div className="settings-rows" data-row="desktop-plugins">
      <p className="settings-row-note">{t("plugins.body")}</p>
      <div className="manage-inline">
        <button type="button" className="button button-secondary" onClick={() => void again()}>
          {t("plugins.rescan")}
        </button>
      </div>
      {records.length === 0 && <p className="manage-muted">{t("plugins.empty")}</p>}
      {records.map((record) => {
        const name = record.name ?? record.id;
        const switchable = record.status === "loaded" || record.status === "disabled";
        return (
          <div className="manage-card" key={record.id} data-plugin={record.id}>
            <div className="manage-inline">
              <strong>{name}</strong>
              <span className="manage-badge" data-tone={TONES[record.status]}>
                {t(STATUS_KEYS[record.status])}
              </span>
              <span className="manage-muted">
                {record.kind === "package"
                  ? t("plugins.kind.package", { name: record.packageName ?? record.id })
                  : t("plugins.kind.disk")}
              </span>
              {switchable && (
                <label className="manage-inline">
                  <input
                    type="checkbox"
                    role="switch"
                    aria-label={t("plugins.enable", { name })}
                    aria-checked={record.status === "loaded"}
                    checked={record.status === "loaded"}
                    onChange={(event) => void toggle(record, event.currentTarget.checked)}
                  />
                  {t("plugins.on")}
                </label>
              )}
            </div>
            {record.description !== undefined && <p>{record.description}</p>}
            <p className="manage-muted">{record.file}</p>
            {record.error !== undefined && (
              <p className="manage-muted" role="note">
                {t("plugins.reason", { reason: record.error })}
              </p>
            )}
            {record.status === "loaded" && <PluginSettings id={record.id} />}
          </div>
        );
      })}
      {note !== "" && (
        <p className="settings-row-note" role="alert">
          {note}
        </p>
      )}
    </div>
  );
}
