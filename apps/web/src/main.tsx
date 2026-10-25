// Browser entry: outer query cache (shell data), router with the injected base path, fonts and
// tokens (the theme provider itself lives in App).

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { lazy, StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router";
import { App } from "./App.tsx";
import { basePath } from "./lib/bootstrap.ts";
import { exposePluginSdk } from "./plugins/registry.ts";
import { buildDashboardSdk } from "./plugins/sdk.ts";
import { AuthWidget } from "./shell/AuthWidget.tsx";
import "./theme/fonts.ts";
import "./index.css";

// The chat terminal (P18-26) and xterm load only when /chat is first visited.
const ChatHost = lazy(() => import("./chat/ChatHost.tsx"));

const client = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } },
});

// Dashboard plugins find their registry and SDK on `window` before anything renders (P24-37).
exposePluginSdk(buildDashboardSdk());

const root = document.getElementById("indus-dashboard");
if (root) {
  const base = basePath();
  createRoot(root).render(
    <StrictMode>
      <QueryClientProvider client={client}>
        <BrowserRouter {...(base ? { basename: base } : {})}>
          <App chatHost={ChatHost} sidebarFooter={<AuthWidget />} />
        </BrowserRouter>
      </QueryClientProvider>
    </StrictMode>,
  );
}
