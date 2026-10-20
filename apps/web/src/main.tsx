// Browser entry: outer query cache (shell data), router with the injected base path, theme tokens.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router";
import { App } from "./App.tsx";
import { basePath } from "./lib/bootstrap.ts";
import { ThemeStyles } from "./shell/ThemeSwitch.tsx";
import "./index.css";

const client = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } },
});

const root = document.getElementById("indus-dashboard");
if (root) {
  const base = basePath();
  createRoot(root).render(
    <StrictMode>
      <QueryClientProvider client={client}>
        <BrowserRouter {...(base ? { basename: base } : {})}>
          <ThemeStyles />
          <App />
        </BrowserRouter>
      </QueryClientProvider>
    </StrictMode>,
  );
}
