import { defineConfig } from "vitest/config";

// Performance budgets (P25-13): wall-clock checks, run alone with `pnpm --filter @indus/tui bench`,
// never in the unit suite (`*.bench.ts` is outside the default include).
export default defineConfig({
  test: {
    include: ["bench/**/*.bench.ts"],
    testTimeout: 60_000,
    fileParallelism: false,
  },
});
