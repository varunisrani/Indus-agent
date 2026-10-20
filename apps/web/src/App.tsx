// The dashboard shell (P18-17): sidebar, header, routed pages, the persistent chat host, banners
// and toasts. Providers that need the browser (router, outer query cache) are set up in main.tsx
// so tests can supply memory equivalents.

import { useQuery } from "@tanstack/react-query";
import { type ComponentType, type ReactNode, Suspense, useEffect, useState } from "react";
import { Navigate, Route, Routes, useLocation } from "react-router";
import { api } from "./lib/api.ts";
import { embeddedChatEnabled } from "./lib/bootstrap.ts";
import type { ConfigObject } from "./lib/restTypes.ts";
import { type ChatHostProps, ChatHostSlot, ChatPending } from "./shell/ChatHostSlot.tsx";
import { PageHeader } from "./shell/PageHeader.tsx";
import {
  ProfileBanner,
  ProfileKeyed,
  ProfileScopeProvider,
  useProfileScope,
} from "./shell/ProfileScope.tsx";
import { CHAT_PATH, DEFAULT_PATH, type PageRoute, ROUTES, routeFor } from "./shell/routes.ts";
import { readCollapsed, Sidebar, useWideScreen, writeCollapsed } from "./shell/Sidebar.tsx";
import { ToastProvider } from "./shell/Toasts.tsx";

export interface AppProps {
  /** The chat terminal host (P18-26); a placeholder until it lands. */
  chatHost?: ComponentType<ChatHostProps>;
  /** Sidebar footer: status strip (P18-25) and auth widget (P18-28). */
  sidebarFooter?: ReactNode;
}

function analyticsFlag(config: ConfigObject | undefined): boolean {
  const dashboard = config?.dashboard;
  if (typeof dashboard !== "object" || dashboard === null || Array.isArray(dashboard)) return false;
  return (dashboard as ConfigObject).show_token_analytics === true;
}

function RouteFallback() {
  return (
    <p className="p-6 text-sm text-muted" aria-busy="true">
      Loading…
    </p>
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

  const config = useQuery({
    queryKey: ["shell", "config", scope.active],
    queryFn: () => api.config.get(),
    staleTime: 60_000,
  });
  const showAnalytics = analyticsFlag(config.data);

  const routes = ROUTES.filter((r) => r.path !== CHAT_PATH || chatEnabled);
  const navItems: PageRoute[] = routes.filter(
    (r) => r.nav === "main" || (r.nav === "analytics-flag" && showAnalytics),
  );
  const current = routeFor(location.pathname);
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
      className="rounded-md border border-line px-2 py-1 text-xs"
      aria-label="Open navigation"
      onClick={() => setMobileOpen(true)}
    >
      Menu
    </button>
  );

  return (
    <div className="flex h-full min-h-0 bg-bg text-fg">
      <Sidebar
        items={navItems}
        search={scope.search}
        wide={wide}
        collapsed={collapsed}
        onToggleCollapsed={toggleCollapsed}
        mobileOpen={mobileOpen}
        onCloseMobile={() => setMobileOpen(false)}
        footer={sidebarFooter}
      />
      <div className="flex min-w-0 flex-1 flex-col">
        <PageHeader title={title} start={menuButton}>
          <ProfileBanner />
          <main className="flex min-h-0 flex-1 flex-col overflow-auto">
            <ProfileKeyed>
              <Suspense fallback={<RouteFallback />}>
                <Routes>
                  <Route
                    path="/"
                    element={
                      <Navigate to={{ pathname: DEFAULT_PATH, search: location.search }} replace />
                    }
                  />
                  {routes.map(({ path, page: Page }) => (
                    <Route key={path} path={path} element={Page ? <Page /> : null} />
                  ))}
                  <Route
                    path="*"
                    element={
                      <Navigate to={{ pathname: DEFAULT_PATH, search: location.search }} replace />
                    }
                  />
                </Routes>
              </Suspense>
            </ProfileKeyed>
            {chatEnabled ? <ChatHostSlot active={onChat} host={chatHost} /> : null}
          </main>
        </PageHeader>
      </div>
    </div>
  );
}

export function App({ chatHost = ChatPending, sidebarFooter }: AppProps) {
  return (
    <ToastProvider>
      <ProfileScopeProvider>
        <Shell chatHost={chatHost} sidebarFooter={sidebarFooter} />
      </ProfileScopeProvider>
    </ToastProvider>
  );
}
