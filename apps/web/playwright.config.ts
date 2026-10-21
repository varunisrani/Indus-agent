import { defineConfig } from "@playwright/test";

// The browser suite (P18-30): each test starts its own stack (fake model, temp home, daemon,
// `indus dashboard`) through `e2e/fixtures.ts`, so tests run one at a time. Chromium only; the
// browser comes from PLAYWRIGHT_BROWSERS_PATH (CI installs it; this suite never downloads one).
export default defineConfig({
  testDir: "e2e",
  testMatch: "*.spec.ts",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 180_000,
  expect: { timeout: 30_000 },
  forbidOnly: !!process.env.CI,
  reporter: [["list"]],
  outputDir: "test-results",
  use: {
    browserName: "chromium",
    headless: true,
    trace: "retain-on-failure",
    viewport: { width: 1280, height: 800 },
  },
});
