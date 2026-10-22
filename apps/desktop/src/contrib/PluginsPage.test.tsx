// @vitest-environment jsdom
import { act, fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { IndusPluginFile } from "../global.d.ts";
import { PluginLoader } from "./loader.ts";
import { PluginsPage } from "./PluginsPage.tsx";
import { ContributionRegistry } from "./registry.ts";
import { fakeHost } from "./testHost.ts";

const refused: IndusPluginFile = {
  id: "grabby",
  kind: "disk",
  file: "/home/u/.indus/desktop-plugins/grabby.js",
  size: 10,
  hash: "c".repeat(64),
  status: "refused",
  reason: 'imports "fs", which plugins may not use',
};

describe("Settings › Advanced › Plugins", () => {
  it("a refused file shows main's reason and no switch; the rescan reloads the list", async () => {
    const loader = new PluginLoader({
      host: fakeHost(),
      registry: new ContributionRegistry(),
      importModule: async () => ({}),
    });
    await loader.sync([refused]);
    let rescans = 0;
    render(
      <PluginsPage
        loader={loader}
        rescan={async () => {
          rescans += 1;
          return { files: [] };
        }}
      />,
    );
    expect(screen.getByText("Refused")).toBeTruthy();
    expect(screen.getByText(`Why: ${refused.reason}`)).toBeTruthy();
    expect(screen.queryByRole("switch")).toBeNull();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Check the folder again" }));
    });
    expect(rescans).toBe(1);
    expect(screen.getByText("No desktop plugins are installed.")).toBeTruthy();
  });

  it("without a running loader it says where plugins are available", () => {
    render(<PluginsPage loader={null} />);
    expect(
      screen.getByText("Desktop plugins are available in the desktop app's main window."),
    ).toBeTruthy();
  });
});
