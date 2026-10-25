// The Plugins tab (P24-26, W52-k): one table row per plugin name with its Desktop and Agent
// halves, each with its own switch (an em dash when the plugin has no such half). "Install from
// Git" opens a dialog that needs a full 40-character commit. We serve no uninstall route and no
// catalog, so there is no trash icon and the catalog section starts collapsed with a note.

import { AlertCircle, Monitor, Package, RefreshCw } from "lucide-react";
import { type FormEvent, useState } from "react";
import { type MessageKey, useT } from "../../i18n/index.tsx";
import { type RestLike, rest } from "../../settings/settingsApi.ts";
import { Button, EmptyState, IconButton, SmallDialog } from "../../ui/components.tsx";
import { PanelPill } from "../kit.tsx";
import { BottomSection, RowSwitch } from "../masterDetail.tsx";
import { notify } from "../Toasts.tsx";
import {
  type AgentPluginRow,
  type DesktopPluginRow,
  FULL_SHA,
  field,
  mergePlugins,
  type PluginBadge,
  useList,
} from "./shared.ts";

const BADGE_KEYS: Record<PluginBadge, MessageKey> = {
  agent: "plugins.badge.agent",
  desktop: "plugins.badge.desktop",
  both: "plugins.badge.both",
};

export function PluginsTab(props: {
  api: RestLike | null;
  profile: string;
  /** The desktop column (app-level, not reloaded on a profile switch). */
  desktop: readonly DesktopPluginRow[];
  /** Turn a desktop plugin on or off; absent: the Desktop switch is read-only. */
  onDesktopToggle?: (id: string, on: boolean) => void;
  /** Read both lists again. */
  onRescan?: () => void;
  /** The profile picker for the Agent column header (several profiles). */
  scope?: React.ReactNode;
}) {
  const t = useT();
  const { api, profile } = props;
  const list = useList<AgentPluginRow>(api, "/api/plugins", profile, field("plugins"));
  const [installing, setInstalling] = useState(false);
  const [status, setStatus] = useState("");
  const rows = mergePlugins(list.rows ?? [], props.desktop);

  const toggle = async (name: string, on: boolean) => {
    list.setRows((all) => all.map((row) => (row.name === name ? { ...row, enabled: on } : row)));
    const answer = await rest(
      api,
      "POST",
      `/api/plugins/${encodeURIComponent(name)}/${on ? "enable" : "disable"}`,
      { profile },
    );
    if (!answer.ok) {
      notify({
        tone: "error",
        title: t("capabilities.toggleFailed", { name }),
        message: answer.message,
      });
    }
    list.reload();
  };

  const dash = (
    <span className="cap-dash" aria-hidden="true">
      —
    </span>
  );

  return (
    <div className="cap-plugins" data-testid="tab-plugins">
      <div className="cap-plugins-top">
        <p className="cap-blurb">{t("plugins.blurb")}</p>
        <span className="cap-toolbar">
          <Button variant="secondary" size="md" onClick={() => setInstalling(true)}>
            {t("plugins.installGit")}
          </Button>
          <IconButton
            label={t("plugins.rescanAll")}
            icon={<RefreshCw aria-hidden="true" size={14} />}
            onClick={() => {
              list.reload();
              props.onRescan?.();
            }}
          />
        </span>
      </div>
      {list.error !== null ? (
        <EmptyState
          look="panel"
          fill
          role="alert"
          icon={<AlertCircle aria-hidden="true" />}
          title={t("capabilities.loadFailed")}
          description={list.error}
          action={
            <Button variant="outline" size="sm" onClick={list.reload}>
              {t("capabilities.refresh")}
            </Button>
          }
        />
      ) : (
        <div className="cap-table-wrap">
          <table className="cap-table">
            <thead>
              <tr>
                <th />
                <th className="cap-col">
                  <span className="cap-col-head">
                    <Monitor aria-hidden="true" size={12} />
                    {t("plugins.desktop")}
                  </span>
                </th>
                <th className="cap-col">
                  <span className="cap-col-head">
                    <Package aria-hidden="true" size={12} />
                    {props.scope ?? (
                      <span className="cap-col-text">{t("plugins.agentIn", { profile })}</span>
                    )}
                  </span>
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => {
                const version = row.agent?.version ?? row.desktop?.version ?? null;
                return (
                  <tr key={row.name} data-testid={`plugin-${row.name}`}>
                    <td>
                      <div className="cap-plugin-line">
                        <span className="cap-plugin-name">{row.name}</span>
                        {version !== null && (
                          <span className="cap-plugin-version">{`v${version.replace(/^v/, "")}`}</span>
                        )}
                        <span className="cap-kind-badge" data-badge={row.badge}>
                          {row.badge !== "agent" && <Monitor aria-hidden="true" size={10} />}
                          {row.badge !== "desktop" && <Package aria-hidden="true" size={10} />}
                          {t(BADGE_KEYS[row.badge])}
                        </span>
                        {row.agent?.source === "bundled" && (
                          <PanelPill>{t("plugins.bundled")}</PanelPill>
                        )}
                      </div>
                      {row.agent !== null && row.agent.description !== "" && (
                        <p className="cap-plugin-desc">{row.agent.description}</p>
                      )}
                    </td>
                    <td className="cap-col">
                      {row.desktop === null ? (
                        dash
                      ) : (
                        <RowSwitch
                          label={t("plugins.enabled", {
                            name: `${row.name} (${t("plugins.desktop")})`,
                          })}
                          checked={row.desktop.enabled}
                          {...(props.onDesktopToggle === undefined
                            ? { disabled: true, title: t("plugins.desktopReadOnly") }
                            : {})}
                          onChange={(on) => {
                            if (row.desktop !== null) props.onDesktopToggle?.(row.desktop.id, on);
                          }}
                        />
                      )}
                    </td>
                    <td className="cap-col">
                      {row.agent === null ? (
                        dash
                      ) : (
                        <RowSwitch
                          label={t("plugins.enabled", { name: row.name })}
                          checked={row.agent.enabled}
                          onChange={(on) => void toggle(row.name, on)}
                        />
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {list.rows !== null && rows.length === 0 && (
            <div className="cap-plugins-empty">
              <p>{t("plugins.none")}</p>
              <p>{t("plugins.noneHint")}</p>
            </div>
          )}
        </div>
      )}
      <BottomSection
        id="capabilities.pluginCatalog"
        label={t("plugins.catalog")}
        upper
        hideLabel={t("plugins.catalog.hide")}
        showLabel={t("plugins.catalog.show")}
        hint={t("plugins.catalog.hint")}
        defaultCollapsed
        defaultHeight={200}
      >
        <p className="cap-hub-empty">{t("plugins.catalog.empty")}</p>
      </BottomSection>
      <p className="sr-only" role="status">
        {status}
      </p>
      {installing && (
        <InstallDialog
          api={api}
          profile={profile}
          onClose={() => setInstalling(false)}
          onInstalled={(name) => {
            setInstalling(false);
            setStatus(t("plugins.installed", { name }));
            notify({ tone: "success", title: t("plugins.installed", { name }) });
            list.reload();
          }}
        />
      )}
    </div>
  );
}

function InstallDialog(props: {
  api: RestLike | null;
  profile: string;
  onClose: () => void;
  onInstalled: (name: string) => void;
}) {
  const t = useT();
  const [source, setSource] = useState("");
  const [sha, setSha] = useState("");
  const [shaError, setShaError] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const install = async (event: FormEvent) => {
    event.preventDefault();
    if (!FULL_SHA.test(sha.trim())) {
      setShaError(true);
      return;
    }
    setShaError(false);
    setBusy(true);
    const answer = await rest<{ name?: string }>(props.api, "POST", "/api/plugins/install", {
      body: { url: source.trim(), sha: sha.trim().toLowerCase() },
      profile: props.profile,
      timeoutMs: 300_000,
    });
    setBusy(false);
    if (answer.ok) props.onInstalled(answer.data.name ?? source.trim());
    else setFailure(answer.message);
  };

  return (
    <SmallDialog title={t("plugins.installGit")} onClose={props.onClose}>
      <form className="cap-form" onSubmit={install}>
        <label className="cap-form-field">
          <span>{t("plugins.source")}</span>
          <input
            className="desk-input"
            aria-label={t("plugins.source")}
            value={source}
            onChange={(event) => setSource(event.target.value)}
          />
        </label>
        <label className="cap-form-field">
          <span>{t("plugins.sha")}</span>
          <input
            className="desk-input cap-mono"
            aria-label={t("plugins.sha")}
            value={sha}
            onChange={(event) => setSha(event.target.value)}
          />
        </label>
        {shaError && (
          <p role="alert" className="md-status" data-tone="bad">
            {t("plugins.badSha")}
          </p>
        )}
        {failure !== null && (
          <p role="alert" className="md-status" data-tone="bad">
            {failure}
          </p>
        )}
        <div className="desk-dialog-actions">
          <Button variant="ghost" size="sm" onClick={props.onClose}>
            {t("capabilities.cancel")}
          </Button>
          <Button type="submit" variant="primary" size="sm" disabled={source.trim() === "" || busy}>
            {t("plugins.installButton")}
          </Button>
        </div>
      </form>
    </SmallDialog>
  );
}
