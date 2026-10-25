// The dashboard shell (P18-17): sidebar, header, routed pages, the persistent chat host, banners
// and toasts. Providers that need the browser (router, outer query cache) are set up in main.tsx
// so tests can supply memory equivalents.

import { Menu } from "lucide-react";
import { type ComponentType, type ReactNode, Suspense, useEffect, useMemo, useState } from "react";
import { Navigate, Route, Routes, useLocation } from "react-router";
import { ConsoleEntry } from "./console/ConsoleModal.tsx";
import { consoleEnabled, embeddedChatEnabled } from "./lib/bootstrap.ts";
import { useTokenAnalyticsFlag } from "./lib/useTokenAnalyticsFlag.ts";
import { mergePluginRoutes, pluginRegistry } from "./plugins/registry.ts";
import { PluginSlot, pluginPageFor, SLOT_PAGES } from "./plugins/slots.tsx";
import { usePluginState } from "./plugins/usePluginState.ts";
import { type ChatHostProps, ChatHostSlot, ChatPending } from "./shell/ChatHostSlot.tsx";
import { PageHeader } from "./shell/PageHeader.tsx";
import { PressureBanner } from "./shell/PressureBanner.tsx";
import {
  ProfileBanner,
  ProfileKeyed,
  ProfileScopeProvider,
  useProfileScope,
} from "./shell/ProfileScope.tsx";
import { CHAT_PATH, DEFAULT_PATH, type PageRoute, ROUTES, routeFor } from "./shell/routes.ts";
import { readCollapsed, Sidebar, useWideScreen, writeCollapsed } from "./shell/Sidebar.tsx";
import { StatusStrip } from "./shell/StatusStrip.tsx";
import { ToastProvider } from "./shell/Toasts.tsx";
import { ThemeProvider } from "./theme/ThemeProvider.tsx";
import { Loading } from "./ui/controls.tsx";

export interface AppProps {
  /** The chat terminal host (P18-26); a placeholder until it lands. */
  chatHost?: ComponentType<ChatHostProps>;
  /** Sidebar footer below the status strip (the auth widget, P18-28). */
  sidebarFooter?: ReactNode;
}

function RouteFallback() {
  return (
    <div className="p-6">
      <Loading />
    </div>
  );
}

function Shell({ chatHost, sidebarFooter }: Required<Pick<AppProps, "chatHost">> & AppProps) {
  const location = useLocation();
  const scope = useProfileScope();
  const wide = useWideScreen();
  const [collapsed, setCollapsed] = useState(readCollapsed);
  const [mobileOpen, setMobileOpen] = useState(false);

  // The drawer closes on Escape and when the window grows past the breakpoint.
  useEffect(() => {
    if (wide) setMobileOpen(false);
  }, [wide]);
  useEffect(() => {
    if (!mobileOpen) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMobileOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [mobileOpen]);
  const chatEnabled = embeddedChatEnabled();

  const showAnalytics = useTokenAnalyticsFlag().enabled;

  const plugins = usePluginState();
  const routes = useMemo(() => {
    const builtin = ROUTES.filter((r) => r.path !== CHAT_PATH || chatEnabled);
    const byName = new Map(plugins.manifests.map((m) => [m.name, m]));
    void plugins.version; // re-merge when a plugin registers late
    return mergePluginRoutes(builtin, plugins.manifests, (name) => {
      const manifest = byName.get(name);
      return manifest && pluginRegistry.component(name) ? pluginPageFor(manifest) : undefined;
    });
  }, [chatEnabled, plugins.manifests, plugins.version]);
  const navItems: PageRoute[] = routes.filter(
    (r) => r.nav === "main" || (r.nav === "analytics-flag" && showAnalytics),
  );
  const current = routes.find((r) => r.path === location.pathname.replace(/(.)\/+$/, "$1"));
  // Until the plugins are in, a plugin tab's deep link waits instead of falling back to the
  // default page, and a page a plugin overrides waits instead of flashing the built-in one.
  const waitForPlugins =
    !plugins.settled &&
    ((location.pathname !== "/" && routeFor(location.pathname) === undefined) ||
      plugins.manifests.some((m) => m.tab.override === location.pathname));
  const pageSlot = SLOT_PAGES.find((page) => location.pathname === `/${page}`);
  const onChat = chatEnabled && current?.path === CHAT_PATH;
  const title = current?.title ?? "Indus Agent";

  const toggleCollapsed = () => {
    setCollapsed((was) => {
      writeCollapsed(!was);
      return !was;
    });
  };

  const menuButton = wide ? null : (
    <button
      type="button"
      className="inline-flex size-7 items-center justify-center text-ink-muted hover:text-ink"
      aria-label="Open navigation"
      onClick={() => setMobileOpen(true)}
    >
      <Menu aria-hidden="true" className="size-4" />
    </button>
  );

  return (
    <div className="flex h-full min-h-0 bg-canvas text-ink">
      <Sidebar
        items={navItems}
        search={scope.search}
        wide={wide}
        collapsed={collapsed}
        onToggleCollapsed={toggleCollapsed}
        mobileOpen={mobileOpen}
        onCloseMobile={() => setMobileOpen(false)}
        footer={(compact) => (
          <>
            <PluginSlot name="sidebar" />
            <StatusStrip compact={compact} />
            <ConsoleEntry enabled={consoleEnabled()} compact={compact} />
            {compact ? null : sidebarFooter}
          </>
        )}
      />
      <div className="flex min-w-0 flex-1 flex-col">
        <PluginSlot name="backdrop" />
        <PageHeader title={title} start={menuButton}>
          <PluginSlot name="header-banner" />
          <ProfileBanner />
          <PressureBanner />
          <main className="flex min-h-0 flex-1 flex-col overflow-auto">
            <PluginSlot name="pre-main" />
            {pageSlot ? <PluginSlot name={`${pageSlot}:top`} /> : null}
            <ProfileKeyed>
              <Suspense fallback={<RouteFallback />}>
                {waitForPlugins ? (
                  <RouteFallback />
                ) : (
                  <Routes>
                    <Route
                      path="/"
                      element={
                        <Navigate
                          to={{ pathname: DEFAULT_PATH, search: location.search }}
                          replace
                        />
                      }
                    />
                    {routes.map(({ path, page: Page }) => (
                      <Route key={path} path={path} element={Page ? <Page /> : null} />
                    ))}
                    <Route
                      path="*"
                      element={
                        <Navigate
                          to={{ pathname: DEFAULT_PATH, search: location.search }}
                          replace
                        />
                      }
                    />
                  </Routes>
                )}
              </Suspense>
            </ProfileKeyed>
            {pageSlot ? <PluginSlot name={`${pageSlot}:bottom`} /> : null}
            <PluginSlot name="post-main" />
            {chatEnabled ? <ChatHostSlot active={onChat} host={chatHost} /> : null}
          </main>
        </PageHeader>
      </div>
      <PluginSlot name="overlay" />
    </div>
  );
}

export function App({ chatHost = ChatPending, sidebarFooter }: AppProps) {
  return (
    <ThemeProvider>
      <ToastProvider>
        <ProfileScopeProvider>
          <Shell chatHost={chatHost} sidebarFooter={sidebarFooter} />
        </ProfileScopeProvider>
      </ToastProvider>
    </ThemeProvider>
  );
}
