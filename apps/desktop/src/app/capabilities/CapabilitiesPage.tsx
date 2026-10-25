// The Capabilities page (P24-26) at the reserved `capabilities` route (P20-15): Skills, Tools,
// Connectors and Plugins for one profile, picked with the scope selector.
//
// W52-k look: no title row; one header with the page search (Skills and Tools), the tabs centred
// with their counts, and the profile scope at the right (several profiles only). The page loads
// the skills and toolsets lists itself (their counts and the search placeholder come from them);
// Connectors and Plugins load their own. Profile-scoped data is keyed by the profile, so a switch
// reloads it; the Desktop column of the Plugins tab is app-level and is read once per page.
// `capabilities.summary` (passed in as `summary`) feeds the counts when the daemon serves it.
// `?tab=skills&install=<identifier>` (an install deep link) opens the Skills tab with a
// confirmation first.

import { useCallback, useEffect, useMemo, useState } from "react";
import { useLocation } from "react-router";
import { type MessageKey, useT } from "../../i18n/index.tsx";
import { bridgeRest, type RestLike, rest } from "../../settings/settingsApi.ts";
import { Button, EmptyState } from "../../ui/components.tsx";
import { desktopBridge } from "../bridge.ts";
import { QuietSearch, TextTabs } from "../kit.tsx";
import { Loader } from "../Loader.tsx";
import { PageHeader, usePlaceholderTerm } from "../masterDetail.tsx";
import "./capabilities.css";
import { ConnectorsTab } from "./ConnectorsTab.tsx";
import { PluginsTab } from "./PluginsTab.tsx";
import { SkillsTab } from "./SkillsTab.tsx";
import {
  type DesktopPluginRow,
  type DesktopPluginSource,
  field,
  type SkillRow,
  type ToolsetRow,
  useList,
} from "./shared.ts";
import { ToolsetsTab } from "./ToolsetsTab.tsx";

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

/** Up to eight distinct search hints from the loaded rows (categories first, then names). */
export function searchTerms(
  tab: CapabilityTab,
  skills: readonly SkillRow[],
  toolsets: readonly ToolsetRow[],
): string[] {
  const words =
    tab === "skills"
      ? [...skills.map((row) => row.category), ...skills.map((row) => row.name)]
      : tab === "toolsets"
        ? toolsets.flatMap((row) => row.tools)
        : [];
  return [...new Set(words.map((word) => word.trim()).filter((word) => word !== ""))].slice(0, 8);
}

export function CapabilitiesPage(props: CapabilitiesPageProps) {
  const t = useT();
  const api = props.api === undefined ? bridgeRest() : props.api;
  const [tab, setTab] = useState<CapabilityTab>(props.initialTab ?? "skills");
  const [profile, setProfile] = useState(props.profile ?? "default");
  const [profiles, setProfiles] = useState<string[]>([props.profile ?? "default"]);
  const [desktop, setDesktop] = useState<DesktopPluginRow[]>([]);
  const [desktopSerial, setDesktopSerial] = useState(0);
  const [counts, setCounts] = useState<CapabilitySummary | null>(null);
  const [pending, setPending] = useState<string | null>(props.pendingInstall ?? null);
  const [query, setQuery] = useState("");
  const skills = useList<SkillRow>(api, "/api/skills", profile, field("skills"));
  const toolsets = useList<ToolsetRow>(api, "/api/tools/toolsets", profile, field("toolsets"));

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

  // the Desktop column: app-level, read once (a profile switch never reloads it; a rescan does)
  // biome-ignore lint/correctness/useExhaustiveDependencies: desktopSerial asks for a rescan
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
  }, [props.desktopPlugins, desktopSerial]);

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

  // Ctrl/Cmd+R inside the page reloads its lists instead of the window
  const { reload: reloadSkills } = skills;
  const { reload: reloadToolsets } = toolsets;
  const reloadAll = useCallback(() => {
    reloadSkills();
    reloadToolsets();
  }, [reloadSkills, reloadToolsets]);
  const onKeyDown = (event: React.KeyboardEvent) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "r") {
      event.preventDefault();
      reloadAll();
    }
  };

  const openUrl =
    props.openUrl ??
    ((url: string) =>
      void desktopBridge()
        ?.external.open({ url })
        .catch(() => {}));

  const terms = useMemo(
    () => searchTerms(tab, skills.rows ?? [], toolsets.rows ?? []),
    [tab, skills.rows, toolsets.rows],
  );
  const term = usePlaceholderTerm(terms);
  const searchable = tab === "skills" || tab === "toolsets";
  const count = (id: CapabilityTab): number | null | undefined => {
    if (id === "skills") return counts?.skills.total ?? skills.rows?.length ?? null;
    if (id === "toolsets") return counts?.toolsets.total ?? toolsets.rows?.length ?? null;
    return undefined;
  };

  const scope =
    profiles.length > 1 ? (
      <select
        className="md-scope"
        aria-label={t("capabilities.scope")}
        value={profile}
        onChange={(event) => setProfile(event.target.value)}
      >
        {profiles.map((name) => (
          <option key={name} value={name}>
            {name}
          </option>
        ))}
      </select>
    ) : null;

  const loadingLists = searchable && (skills.rows === null || toolsets.rows === null);
  const failed = tab === "skills" ? skills.error : tab === "toolsets" ? toolsets.error : null;

  let body: React.ReactNode;
  if (failed !== null) {
    body = (
      <EmptyState
        look="panel"
        fill
        role="alert"
        title={t("capabilities.loadFailed")}
        description={failed}
        action={
          <Button variant="outline" size="sm" onClick={reloadAll}>
            {t("capabilities.refresh")}
          </Button>
        }
      />
    );
  } else if (loadingLists && tab === "skills" && skills.rows === null) {
    body = <Loader />;
  } else if (loadingLists && tab === "toolsets" && toolsets.rows === null) {
    body = <Loader />;
  } else if (tab === "skills") {
    body = (
      <SkillsTab
        key={`skills-${profile}`}
        api={api}
        profile={profile}
        list={skills}
        query={query}
        pendingInstall={pending}
        onPendingDone={() => setPending(null)}
      />
    );
  } else if (tab === "toolsets") {
    body = (
      <ToolsetsTab
        key={`toolsets-${profile}`}
        api={api}
        profile={profile}
        list={toolsets}
        query={query}
      />
    );
  } else if (tab === "connectors") {
    body = (
      <ConnectorsTab key={`connectors-${profile}`} api={api} profile={profile} openUrl={openUrl} />
    );
  } else {
    const source = props.desktopPlugins;
    body = (
      <PluginsTab
        key={`plugins-${profile}`}
        api={api}
        profile={profile}
        desktop={desktop}
        onRescan={() => setDesktopSerial((n) => n + 1)}
        {...(source?.setEnabled === undefined
          ? {}
          : {
              onDesktopToggle: (id: string, on: boolean) => {
                setDesktop((rows) =>
                  rows.map((row) => (row.id === id ? { ...row, enabled: on } : row)),
                );
                void source.setEnabled?.(id, on).finally(() => setDesktopSerial((n) => n + 1));
              },
            })}
      />
    );
  }

  return (
    <section className="md-page" aria-labelledby="page-capabilities" onKeyDown={onKeyDown}>
      <h1 id="page-capabilities" className="sr-only">
        {t("nav.capabilities")}
      </h1>
      <PageHeader
        search={
          searchable ? (
            <QuietSearch
              fill
              label={t("capabilities.search")}
              placeholder={
                term === null ? t("capabilities.search") : t("capabilities.try", { term })
              }
              value={query}
              onChange={setQuery}
            />
          ) : undefined
        }
        tabs={
          <TextTabs
            look="page"
            label={t("capabilities.tabs")}
            value={tab}
            onChange={setTab}
            options={CAPABILITY_TABS.map((id) => ({
              value: id,
              label: t(TAB_KEYS[id]),
              ...(count(id) === undefined ? {} : { count: count(id) }),
            }))}
          />
        }
        trailing={scope}
      />
      <div role="tabpanel" className="md-tabpanel">
        {body}
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
