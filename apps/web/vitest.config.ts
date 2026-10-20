import { defineConfig } from "vitest/config";

// Page and shell tests render React into jsdom against the typed mock backend
// (src/testing/mockApi.ts); nothing here opens a real socket or port.
export default defineConfig({
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    restoreMocks: true,
  },
});
