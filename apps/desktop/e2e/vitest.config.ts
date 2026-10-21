import { defineConfig } from "vitest/config";

// The Electron end-to-end lane (P20-40): `pnpm --filter @indus/desktop e2e`. One file at a time
// (each starts real Electron processes and a fake or real backend), 1 retry on CI reported in the
// summary (R10.20), 90 s per test unless a spec says otherwise. Skips with the reason when there is
// no Electron binary or display; the real-runtime cases also without the repository's `.venv`.
export default defineConfig({
  test: {
    root: import.meta.dirname,
    include: ["core/**/*.electron.test.ts", "startup-budget.spec.ts"],
    environment: "node",
    fileParallelism: false,
    testTimeout: 90_000,
    hookTimeout: 180_000,
    retry: process.env.CI ? 1 : 0,
    reporters: ["default"],
  },
});
