import { defineConfig } from "vitest/config";

// Demo 2 end to end (P12-33): one renderer, bridge child and fake model at a time.
export default defineConfig({
  test: {
    include: ["e2e/**/*.e2e.test.ts"],
    fileParallelism: false,
    testTimeout: 120_000,
    hookTimeout: 60_000,
  },
});
