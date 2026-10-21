// Shared test setup: unmount React trees and reset browser state between tests.
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";
import { setSelectedProfile } from "../lib/api.ts";

afterEach(() => {
  // Build-tool tests (vite.*.test.ts) run in the node environment: no browser state to reset.
  if (typeof window === "undefined") return;
  cleanup();
  setSelectedProfile("");
  window.sessionStorage.clear();
  window.localStorage.clear();
  delete window.__INDUS_SESSION_TOKEN__;
  delete window.__INDUS_BASE_PATH__;
  delete window.__INDUS_AUTH_REQUIRED__;
  delete window.__INDUS_DASHBOARD_EMBEDDED_CHAT__;
  delete window.__INDUS_INITIAL_PROFILE__;
  delete window.__INDUS_DASHBOARD_PROFILE__;
});

// jsdom has no ResizeObserver; Radix measures some controls with it. A no-op observer is enough
// for tests (nothing here depends on layout).
if (typeof globalThis.ResizeObserver === "undefined") {
  globalThis.ResizeObserver = class {
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
  };
}
