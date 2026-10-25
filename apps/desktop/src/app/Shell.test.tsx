// @vitest-environment jsdom
import type { ClientEvent } from "@indus/client";
import { act, fireEvent, render, screen } from "@testing-library/react";
import axe from "axe-core";
import { MemoryRouter, useLocation } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TranscriptStoreProvider } from "../chat/transcriptReact.tsx";
import { type TranscriptSource, TranscriptStore } from "../chat/transcriptStore.ts";
import { AppRoutes } from "./App.tsx";
import { RootErrorBoundary } from "./ErrorBoundary.tsx";
import { ThemeProvider } from "./theme.tsx";

function store(): TranscriptStore {
  const source: TranscriptSource = {
    onEvent: (_listener: (event: ClientEvent) => void) => () => undefined,
    request: (async () => ({ messages: [] })) as unknown as TranscriptSource["request"],
    watchSession: async () => undefined,
    unwatchSession: () => undefined,
  };
  return new TranscriptStore(source);
}

function Where() {
  return <output data-testid="where">{useLocation().pathname}</output>;
}

function mount(path: string) {
  return render(
    <ThemeProvider>
      <TranscriptStoreProvider store={store()}>
        <MemoryRouter initialEntries={[path]}>
          <AppRoutes />
          <Where />
        </MemoryRouter>
      </TranscriptStoreProvider>
    </ThemeProvider>,
  );
}

const where = () => screen.getByTestId("where").textContent;

afterEach(() => {
  delete (globalThis as { indusDesktop?: unknown }).indusDesktop;
  delete document.documentElement.dataset.theme;
});

describe("shell", () => {
  it("has a sidebar, the pane area and a status bar", () => {
    mount("/");
    expect(screen.getByRole("navigation", { name: "Main" })).toBeTruthy();
    expect(screen.getByRole("main")).toBeTruthy();
    expect(screen.getByRole("heading", { name: "INDUS AGENT" })).toBeTruthy();
  });

  it("an unknown route opens a new chat", async () => {
    mount("/a/b/c");
    await act(async () => undefined);
    expect(where()).toBe("/");
  });

  it("an overlay floats over the route it came from and returns there on close", async () => {
    mount("/capabilities");
    expect(screen.getByRole("heading", { name: "Capabilities" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Settings" }));
    expect(where()).toBe("/settings");
    expect(screen.getByRole("dialog", { name: "Settings" })).toBeTruthy();
    // The page stays underneath.
    expect(screen.getByRole("heading", { name: "Capabilities", hidden: true })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(where()).toBe("/capabilities");
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("an overlay opened cold returns home", () => {
    mount("/cron");
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    expect(where()).toBe("/");
  });

  it("the theme control switches tokens and tells main", () => {
    const setTheme = vi.fn();
    (globalThis as { indusDesktop?: unknown }).indusDesktop = { windows: { setTheme } };
    mount("/");
    fireEvent.click(screen.getByRole("radio", { name: "Dark" }));
    expect(document.documentElement.dataset.theme).toBe("indus-dark");
    expect(setTheme).toHaveBeenLastCalledWith({ mode: "dark" });
    fireEvent.click(screen.getByRole("radio", { name: "Light" }));
    expect(document.documentElement.dataset.theme).toBe("indus-light");
    expect(window.localStorage.getItem("indus-desktop-theme")).toBe("light");
  });

  it("a render error shows the recovery surface and goes to the desktop log", () => {
    const rendererError = vi.fn();
    (globalThis as { indusDesktop?: unknown }).indusDesktop = { logs: { rendererError } };
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    function Broken(): never {
      throw new Error("boom");
    }
    render(
      <RootErrorBoundary>
        <Broken />
      </RootErrorBoundary>,
    );
    expect(screen.getByRole("alert").textContent).toContain("Something went wrong in this window");
    expect(rendererError).toHaveBeenCalledWith(
      expect.objectContaining({ message: "boom", source: "renderer:main" }),
    );
  });

  it.each(["Light", "Dark"])("has no axe violations (%s)", async (theme) => {
    const view = mount("/");
    fireEvent.click(screen.getByRole("radio", { name: theme }));
    const results = await axe.run(view.container, {
      // jsdom has no layout or colours: contrast is checked against the tokens elsewhere.
      rules: { "color-contrast": { enabled: false } },
    });
    expect(results.violations.map((v) => `${v.id}: ${v.help}`)).toEqual([]);
  });
});
