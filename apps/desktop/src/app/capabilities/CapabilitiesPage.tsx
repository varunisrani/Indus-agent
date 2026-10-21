// The Capabilities page (P24-26) at the reserved `capabilities` route (P20-15): Skills, Toolsets,
// Connectors and Plugins for one profile, picked with the scope selector.
//
// Profile-scoped tabs are keyed by the profile, so a switch remounts them and they reload for the
// new profile; the Desktop column of the Plugins tab is app-level and is read once per page, never
// again on a switch. The pills count each kind (`capabilities.summary` when the daemon serves it,
// passed in as `summary`; else no count). `?tab=skills&install=<identifier>` (an install deep link)
// opens the Skills tab with a confirmation first.

import { useEffect, useState } from "react";
import { useLocation } from "react-router";
import { type MessageKey, useT } from "../../i18n/index.tsx";
import { bridgeRest, type RestLike, rest } from "../../settings/settingsApi.ts";
import { desktopBridge } from "../bridge.ts";
import {
  ConnectorsTab,
  type DesktopPluginRow,
  type DesktopPluginSource,
  PluginsTab,
  SkillsTab,
  ToolsetsTab,
} from "./tabs.tsx";

export const CAPABILITY_TABS = ["skills", "toolsets", "connectors", "plugins"] as const;
export type CapabilityTab = (typeof CAPABILITY_TABS)[number];

const TAB_KEYS: Record<CapabilityTab, MessageKey> = {
  skills: "capabilities.tab.skills",
  toolsets: "capabilities.tab.toolsets",
  connectors: "capabilities.tab.connectors",
  plugins: "capabilities.tab.plugins",
};

/** `capabilities.summary`'s answer (P24-26 backend). */
export interface CapabilitySummary {
  skills: { total: number; enabled: number };
  toolsets: { total: number; enabled: number };
  mcp_servers: { total: number; enabled: number };
  plugins: { total: number; enabled: number };
}

const SUMMARY_FIELD: Record<CapabilityTab, keyof CapabilitySummary> = {
  skills: "skills",
  toolsets: "toolsets",
  connectors: "mcp_servers",
  plugins: "plugins",
};

export interface CapabilitiesPageProps {
  api?: RestLike | null;
  /** The app-level desktop plugins (U3, P24-32); none: an empty Desktop column. */
  desktopPlugins?: DesktopPluginSource | null;
  /** The profile to start with (the window's foreground profile). */
  profile?: string;
  initialTab?: CapabilityTab;
  pendingInstall?: string | null;
  summary?: (profile: string) => Promise<CapabilitySummary | null>;
  openUrl?: (url: string) => void;
}

export function CapabilitiesPage(props: CapabilitiesPageProps) {
  const t = useT();
  const api = props.api === undefined ? bridgeRest() : props.api;
  const [tab, setTab] = useState<CapabilityTab>(props.initialTab ?? "skills");
  const [profile, setProfile] = useState(props.profile ?? "default");
  const [profiles, setProfiles] = useState<string[]>([props.profile ?? "default"]);
  const [desktop, setDesktop] = useState<DesktopPluginRow[]>([]);
  const [counts, setCounts] = useState<CapabilitySummary | null>(null);
  const [pending, setPending] = useState<string | null>(props.pendingInstall ?? null);

  // the profile list is read once per page (the starting profile stays in it)
  // biome-ignore lint/correctness/useExhaustiveDependencies: once per page and api
  useEffect(() => {
    let live = true;
    void rest<{ profiles?: { name: string }[] }>(api, "GET", "/api/profiles").then((answer) => {
      if (!live || !answer.ok) return;
      const names = (answer.data.profiles ?? []).map((p) => p.name).filter(Boolean);
      if (names.length > 0) setProfiles(names.includes(profile) ? names : [profile, ...names]);
    });
    return () => {
      live = false;
    };
  }, [api]);

  // the Desktop column: app-level, read once (a profile switch never reloads it)
  useEffect(() => {
    let live = true;
    void props.desktopPlugins
      ?.list()
      .then((rows) => {
        if (live) setDesktop(rows);
      })
      .catch(() => {});
    return () => {
      live = false;
    };
  }, [props.desktopPlugins]);

  const { summary } = props;
  useEffect(() => {
    let live = true;
    setCounts(null);
    void summary?.(profile)
      .then((value) => {
        if (live) setCounts(value);
      })
      .catch(() => {});
    return () => {
      live = false;
    };
  }, [summary, profile]);

  useEffect(() => {
    if (props.pendingInstall) {
      setPending(props.pendingInstall);
      setTab("skills");
    }
  }, [props.pendingInstall]);

  const scoped = `${tab}-${profile}`;
  const openUrl =
    props.openUrl ??
    ((url: string) =>
      void desktopBridge()
        ?.external.open({ url })
        .catch(() => {}));

  return (
    <section className="flex h-full min-h-0 flex-col" aria-labelledby="page-capabilities">
      <header className="flex items-center gap-3 border-b border-line px-4 py-2">
        <h1 id="page-capabilities" className="text-base font-semibold">
          {t("nav.capabilities")}
        </h1>
        <div role="tablist" aria-label={t("capabilities.tabs")} className="flex gap-1">
          {CAPABILITY_TABS.map((id) => {
            const count = counts?.[SUMMARY_FIELD[id]];
            return (
              <button
                key={id}
                type="button"
                role="tab"
                aria-selected={tab === id}
                className={`rounded px-2 py-0.5 text-sm ${tab === id ? "bg-raised text-fg" : "text-muted hover:text-fg"}`}
                onClick={() => setTab(id)}
              >
                {t(TAB_KEYS[id])}
                {count !== undefined ? (
                  <span className="ml-1 text-xs text-muted">
                    {t("capabilities.count", { enabled: count.enabled, total: count.total })}
                  </span>
                ) : null}
              </button>
            );
          })}
        </div>
        {profiles.length > 1 ? (
          <label className="ml-auto flex items-center gap-1 text-xs text-muted">
            {t("capabilities.scope")}
            <select
              className="rounded border border-line bg-bg px-1 py-0.5 text-sm text-fg"
              value={profile}
              onChange={(event) => setProfile(event.target.value)}
            >
              {profiles.map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </select>
          </label>
        ) : null}
      </header>
      <div role="tabpanel" className="min-h-0 flex-1 overflow-auto">
        {tab === "skills" ? (
          <SkillsTab
            key={scoped}
            api={api}
            profile={profile}
            pendingInstall={pending}
            onPendingDone={() => setPending(null)}
          />
        ) : tab === "toolsets" ? (
          <ToolsetsTab key={scoped} api={api} profile={profile} />
        ) : tab === "connectors" ? (
          <ConnectorsTab key={scoped} api={api} profile={profile} openUrl={openUrl} />
        ) : (
          <PluginsTab key={scoped} api={api} profile={profile} desktop={desktop} />
        )}
      </div>
    </section>
  );
}

/** The route's page: `?tab=` and `?install=` from the address, the window's profile. */
export function CapabilitiesRoute(
  props: Omit<CapabilitiesPageProps, "initialTab" | "pendingInstall">,
) {
  const location = useLocation();
  const query = new URLSearchParams(location.search);
  const tab = query.get("tab");
  const install = query.get("install");
  return (
    <CapabilitiesPage
      {...props}
      {...(CAPABILITY_TABS.includes(tab as CapabilityTab)
        ? { initialTab: tab as CapabilityTab }
        : {})}
      pendingInstall={install !== null && install.trim() !== "" ? install.trim() : null}
    />
  );
}
