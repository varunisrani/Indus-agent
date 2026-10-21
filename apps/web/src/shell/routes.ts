// The dashboard's route table (P18-17). One row per page; every page is lazy. A page that is not
// built yet uses the `pending(...)` loader until its task swaps in the real import.

import { type ComponentType, createElement, type LazyExoticComponent, lazy } from "react";

export type PageComponent = LazyExoticComponent<ComponentType>;

export type NavPlacement =
  /** Listed in the sidebar. */
  | "main"
  /** Listed only while `dashboard.show_token_analytics` is on. */
  | "analytics-flag"
  /** Routable but never listed (sub-pages such as the profile builder). */
  | "hidden";

export interface PageRoute {
  path: string;
  title: string;
  nav: NavPlacement;
  /** Null for `/chat`, whose terminal lives outside the router (ChatHostSlot). */
  page: PageComponent | null;
}

export interface ExternalLink {
  path: string;
  title: string;
  href: string;
}

export const DEFAULT_PATH = "/sessions";
export const CHAT_PATH = "/chat";

/** Our documentation (the docs domain is still open, P25-20; the repository docs until then). */
export const DOCS_LINK: ExternalLink = {
  path: "/docs",
  title: "Documentation",
  href: "https://github.com/varunisrani/indus-agent/tree/main/docs",
};

/** A placeholder page for a route whose page is not part of this build yet. */
export function pending(title: string): PageComponent {
  return lazy(() =>
    import("./PendingPage.tsx").then((m) => ({
      default: () => createElement(m.PendingPage, { title }),
    })),
  );
}

const SessionsPage = lazy(() =>
  import("../pages/SessionsPage.tsx").then((m) => ({ default: m.SessionsPage })),
);
const FilesPage = lazy(() =>
  import("../pages/FilesPage.tsx").then((m) => ({ default: m.FilesPage })),
);
const AnalyticsPage = lazy(() =>
  import("../pages/AnalyticsPage.tsx").then((m) => ({ default: m.AnalyticsPage })),
);
const LogsPage = lazy(() => import("../pages/LogsPage.tsx").then((m) => ({ default: m.LogsPage })));
const CronPage = lazy(() => import("../pages/CronPage.tsx").then((m) => ({ default: m.CronPage })));
const SkillsPage = lazy(() =>
  import("../pages/SkillsPage.tsx").then((m) => ({ default: m.SkillsPage })),
);
const PluginsPage = lazy(() =>
  import("../pages/PluginsPage.tsx").then((m) => ({ default: m.PluginsPage })),
);
const McpPage = lazy(() => import("../pages/McpPage.tsx").then((m) => ({ default: m.McpPage })));
const ConfigPage = lazy(() =>
  import("../pages/ConfigPage.tsx").then((m) => ({ default: m.ConfigPage })),
);
const EnvPage = lazy(() => import("../pages/EnvPage.tsx").then((m) => ({ default: m.EnvPage })));
const ModelsPage = lazy(() =>
  import("../pages/ModelsPage.tsx").then((m) => ({ default: m.ModelsPage })),
);
const ChannelsPage = lazy(() =>
  import("../pages/ChannelsPage.tsx").then((m) => ({ default: m.ChannelsPage })),
);
const WebhooksPage = lazy(() =>
  import("../pages/WebhooksPage.tsx").then((m) => ({ default: m.WebhooksPage })),
);
const PairingPage = lazy(() =>
  import("../pages/PairingPage.tsx").then((m) => ({ default: m.PairingPage })),
);
const ProfilesPage = lazy(() =>
  import("../pages/ProfilesPage.tsx").then((m) => ({ default: m.ProfilesPage })),
);
const ProfileBuilderPage = lazy(() =>
  import("../pages/ProfileBuilderPage.tsx").then((m) => ({ default: m.ProfileBuilderPage })),
);
const SystemPage = lazy(() =>
  import("../pages/SystemPage.tsx").then((m) => ({ default: m.SystemPage })),
);

export const ROUTES: readonly PageRoute[] = [
  { path: CHAT_PATH, title: "Chat", nav: "main", page: null },
  { path: "/sessions", title: "Sessions", nav: "main", page: SessionsPage },
  { path: "/files", title: "Files", nav: "main", page: FilesPage },
  { path: "/analytics", title: "Analytics", nav: "analytics-flag", page: AnalyticsPage },
  { path: "/models", title: "Models", nav: "main", page: ModelsPage },
  { path: "/logs", title: "Logs", nav: "main", page: LogsPage },
  { path: "/cron", title: "Cron", nav: "main", page: CronPage },
  { path: "/skills", title: "Skills", nav: "main", page: SkillsPage },
  { path: "/plugins", title: "Plugins", nav: "main", page: PluginsPage },
  { path: "/mcp", title: "MCP", nav: "main", page: McpPage },
  { path: "/channels", title: "Channels", nav: "main", page: ChannelsPage },
  { path: "/webhooks", title: "Webhooks", nav: "main", page: WebhooksPage },
  { path: "/pairing", title: "Pairing", nav: "main", page: PairingPage },
  { path: "/profiles", title: "Profiles", nav: "main", page: ProfilesPage },
  { path: "/profiles/new", title: "New profile", nav: "hidden", page: ProfileBuilderPage },
  { path: "/config", title: "Config", nav: "main", page: ConfigPage },
  { path: "/env", title: "Keys", nav: "main", page: EnvPage },
  { path: "/system", title: "System", nav: "main", page: SystemPage },
];

/** The route for a pathname (exact match), if any. */
export function routeFor(pathname: string): PageRoute | undefined {
  const bare = pathname.length > 1 ? pathname.replace(/\/+$/, "") : pathname;
  return ROUTES.find((route) => route.path === bare);
}
