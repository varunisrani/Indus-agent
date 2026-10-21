import { describe, expect, it } from "vitest";
import { pluginRows } from "./host.ts";

describe("the Capabilities page's desktop plugin rows (P24-26 over P24-32)", () => {
  it("a loaded plugin is on; any other state is off; the id names one without a name", () => {
    const rows = pluginRows([
      { id: "weather", kind: "disk", file: "weather.js", status: "loaded", name: "Weather" },
      { id: "notes", kind: "disk", file: "notes.js", status: "disabled" },
      { id: "bad", kind: "package", file: "bad.js", status: "error", error: "boom" },
    ]);
    expect(rows).toEqual([
      { id: "weather", name: "Weather", enabled: true },
      { id: "notes", name: "notes", enabled: false },
      { id: "bad", name: "bad", enabled: false },
    ]);
  });
});
