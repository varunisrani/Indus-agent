// @vitest-environment jsdom
// The shell under each interface mode (P20-27): Simple hides the status bar and, with a single
// profile, the profile rail; switching back restores the Advanced arrangement and surfaces.
import type { ClientEvent } from "@indus/client";
import { act, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it } from "vitest";
import { AppRoutes } from "../app/App.tsx";
import { ShellChromeProvider } from "../app/Shell.tsx";
import { ThemeProvider } from "../app/theme.tsx";
import { TranscriptStoreProvider } from "../chat/transcriptReact.tsx";
import { type TranscriptSource, TranscriptStore } from "../chat/transcriptStore.ts";
import { SidebarModel } from "../sidebar/Sidebar.tsx";
import { LayoutStore } from "./layoutStore.ts";

function transcripts(): TranscriptStore {
  const source: TranscriptSource = {
    onEvent: (_listener: (event: ClientEvent) => void) => () => undefined,
    request: (async () => ({ messages: [] })) as unknown as TranscriptSource["request"],
    watchSession: async () => undefined,
    unwatchSession: () => undefined,
  };
  return new TranscriptStore(source);
}

function bridge(profiles: string[]) {
  (globalThis as { indusDesktop?: unknown }).indusDesktop = {
    windows: { setTheme: () => undefined },
    api: {
      request: async (payload: { path: string }) =>
        payload.path === "/api/profiles"
          ? {
              kind: "response",
              status: 200,
              body: { profiles: profiles.map((name) => ({ name })) },
            }
          : { kind: "response", status: 200, body: { sessions: [] } },
    },
    profile: {
      get: async () => ({ active: null, defaultProfile: null, launch: "default" }),
      onChanged: () => () => undefined,
    },
  };
}

function mount() {
  const layout = new LayoutStore({ available: () => ["chat"] });
  const sidebar = new SidebarModel({ api: null, storage: null, listDir: null });
  render(
    <ThemeProvider>
      <TranscriptStoreProvider store={transcripts()}>
        <ShellChromeProvider value={{ layout, sidebar }}>
          <MemoryRouter initialEntries={["/"]}>
            <AppRoutes />
          </MemoryRouter>
        </ShellChromeProvider>
      </TranscriptStoreProvider>
    </ThemeProvider>,
  );
  return { layout };
}

afterEach(() => {
  delete (globalThis as { indusDesktop?: unknown }).indusDesktop;
});

describe("interface modes in the shell", () => {
  it("Simple hides the status bar and a lone profile's rail; Advanced brings them back", async () => {
    bridge(["default"]);
    const { layout } = mount();
    expect(await screen.findByRole("navigation", { name: "Profiles" })).toBeTruthy();
    expect(screen.getByRole("contentinfo")).toBeTruthy();
    act(() => layout.setMode("simple"));
    expect(screen.queryByRole("navigation", { name: "Profiles" })).toBeNull();
    expect(screen.queryByRole("contentinfo")).toBeNull();
    expect(document.querySelector(".shell")?.getAttribute("data-interface-mode")).toBe("simple");
    act(() => layout.setMode("advanced"));
    expect(screen.getByRole("navigation", { name: "Profiles" })).toBeTruthy();
    expect(screen.getByRole("contentinfo")).toBeTruthy();
  });

  it("Simple keeps the rail when there are several profiles", async () => {
    bridge(["default", "work"]);
    const { layout } = mount();
    act(() => layout.setMode("simple"));
    await waitFor(() => expect(screen.getByRole("navigation", { name: "Profiles" })).toBeTruthy());
    expect(screen.getAllByRole("button", { name: /^Profile / })).toHaveLength(2);
  });
});
