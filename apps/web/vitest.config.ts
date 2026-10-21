import { configDefaults, defineConfig } from "vitest/config";

// Page and shell tests render React into jsdom against the typed mock backend
// (src/testing/mockApi.ts); nothing here opens a real socket or port. The browser suite in e2e/
// runs under Playwright (`pnpm --filter @indus/web e2e`), not here.
export default defineConfig({
  test: {
    environment: "jsdom",
    exclude: [...configDefaults.exclude, "e2e/**"],
    setupFiles: ["./src/test/setup.ts"],
    restoreMocks: true,
  },
});
