// Test helpers: render the whole shell or one page inside memory equivalents of the browser
// providers main.tsx sets up, against whatever `fetch` the test installed (usually mockApi).

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactElement, ReactNode } from "react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { PageHeader } from "../shell/PageHeader.tsx";
import { ProfileScopeProvider } from "../shell/ProfileScope.tsx";
import { ToastProvider } from "../shell/Toasts.tsx";

export function testClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, refetchOnWindowFocus: false },
      mutations: { retry: false },
    },
  });
}

let lastLocation = "";

/** The router location the last render saw (path + search). */
export function currentLocation(): string {
  return lastLocation;
}

export function LocationProbe() {
  const location = useLocation();
  lastLocation = `${location.pathname}${location.search}`;
  return null;
}

/** Render one page at `path`; any other route the page navigates to renders a marker. */
export function renderPage(page: ReactElement, path = "/") {
  const routePath = path.split("?")[0] ?? "/";
  return render(
    <QueryClientProvider client={testClient()}>
      <MemoryRouter initialEntries={[path]}>
        <ToastProvider>
          <ProfileScopeProvider>
            <PageHeader title="Page under test">
              <Routes>
                <Route path={routePath} element={page} />
                <Route path="*" element={<p data-testid="elsewhere">elsewhere</p>} />
              </Routes>
            </PageHeader>
            <LocationProbe />
          </ProfileScopeProvider>
        </ToastProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

export function Wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={testClient()}>{children}</QueryClientProvider>;
}
