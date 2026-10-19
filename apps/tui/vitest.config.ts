import { configDefaults, defineConfig } from "vitest/config";

// The Demo 2 end-to-end suite (`e2e/`) drives the built app in a pseudo-terminal and takes about
// a minute; it runs only through `pnpm --filter @indus/tui run e2e` (see vitest.e2e.config.ts).
export default defineConfig({
  test: {
    exclude: [...configDefaults.exclude, "e2e/**"],
  },
});
