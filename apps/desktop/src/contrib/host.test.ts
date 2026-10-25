import { describe, expect, it } from "vitest";
import { pluginRows, pluginSourceFor } from "./host.ts";
import type { PluginRecord } from "./loader.ts";

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

describe("the Desktop column's switch (W53: setEnabled over the loader)", () => {
  it("applies the choice through the loader, and the next read shows it", async () => {
    let records: PluginRecord[] = [
      { id: "weather", kind: "disk", file: "weather.js", status: "loaded", name: "Weather" },
    ];
    const calls: Array<[string, boolean]> = [];
    const source = pluginSourceFor(() => ({
      inventory: () => records,
      setEnabled: async (id: string, on: boolean) => {
        calls.push([id, on]);
        records = records.map((r) =>
          r.id === id ? { ...r, status: on ? "loaded" : "disabled" } : r,
        );
      },
    }));
    await source.setEnabled?.("weather", false);
    expect(calls).toEqual([["weather", false]]);
    expect(await source.list()).toEqual([{ id: "weather", name: "Weather", enabled: false }]);
  });

  it("is a quiet no-op before the plugins started", async () => {
    const source = pluginSourceFor(() => null);
    await expect(source.setEnabled?.("weather", true)).resolves.toBeUndefined();
    expect(await source.list()).toEqual([]);
  });
});
