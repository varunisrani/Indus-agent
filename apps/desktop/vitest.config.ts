import { defineConfig } from "vitest/config";

// Main-process modules are pure or take their Electron objects as arguments, so they run under
// plain Node with fakes. `*.electron.test.ts` files start a real Electron binary and skip (with
// the reason) when it is not downloaded or no display is available.
export default defineConfig({
  test: {
    include: ["electron/**/*.test.ts", "scripts/**/*.test.ts", "testing/**/*.test.ts"],
    environment: "node",
    restoreMocks: true,
    testTimeout: 20_000,
    hookTimeout: 60_000,
  },
});
