// @vitest-environment jsdom
import { semanticNames } from "@indus/ui";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { createElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AppearanceStore } from "../theme/themeStore.ts";
import {
  CommandPalette,
  ContributedTitleBar,
  keybindFor,
  mirrorThemeContributions,
  useContributedKeybinds,
} from "./areas.tsx";
import { ContributionRegistry } from "./registry.ts";

const LINUX = "linux";

function key(init: Partial<KeyboardEventInit> & { key: string }): KeyboardEvent {
  return new KeyboardEvent("keydown", { bubbles: true, cancelable: true, ...init });
}

function colours(hex: string): Record<string, string> {
  return Object.fromEntries(semanticNames.map((name) => [name, hex]));
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("title-bar area", () => {
  it("draws nothing while empty, then the items in order; a throwing item hides alone", () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const registry = new ContributionRegistry();
    render(createElement(ContributedTitleBar, { platform: LINUX, bandHeight: 34, registry }));
    expect(screen.queryByRole("toolbar")).toBeNull();
    act(() => {
      registry.register({
        id: "p:sync",
        area: "title-bar",
        source: "plugin:p",
        render: () => createElement("span", null, "synced"),
      });
      registry.register({
        id: "q:bad",
        area: "title-bar",
        source: "plugin:q",
        render: () => {
          throw new Error("broken");
        },
      });
    });
    const bar = screen.getByRole("toolbar");
    expect(bar.textContent).toBe("synced");
    expect(bar.dataset.platform).toBe("other");
    act(() => registry.removeSource("plugin:p"));
    expect(screen.queryByText("synced")).toBeNull();
  });
});

describe("keybind area", () => {
  const registry = new ContributionRegistry();
  const run = vi.fn();
  registry.register({
    id: "p:mine",
    area: "keybind",
    source: "plugin:p",
    keys: "Mod+Shift+K",
    run,
  });
  registry.register({ id: "p:tab", area: "keybind", source: "plugin:p", keys: "Mod+T", run });
  registry.register({ id: "p:new", area: "keybind", source: "plugin:p", keys: "Ctrl+N", run });
  registry.register({
    id: "p:palette",
    area: "keybind",
    source: "plugin:p",
    keys: "Mod+Shift+P",
    run,
  });
  const list = registry.list("keybind");

  it("matches its accelerator on the platform modifier", () => {
    expect(keybindFor(list, key({ key: "K", ctrlKey: true, shiftKey: true }), LINUX)?.id).toBe(
      "p:mine",
    );
    expect(keybindFor(list, key({ key: "K", metaKey: true, shiftKey: true }), "darwin")?.id).toBe(
      "p:mine",
    );
    expect(keybindFor(list, key({ key: "J", ctrlKey: true, shiftKey: true }), LINUX)).toBeNull();
  });

  it("never takes a key the app uses (pane shortcut, action default, the palette)", () => {
    expect(keybindFor(list, key({ key: "t", ctrlKey: true }), LINUX)).toBeNull();
    expect(keybindFor(list, key({ key: "n", ctrlKey: true }), LINUX)).toBeNull();
    expect(keybindFor(list, key({ key: "P", ctrlKey: true, shiftKey: true }), LINUX)).toBeNull();
  });

  it("runs on the window's keydown; a throwing run only warns", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const live = new ContributionRegistry();
    const ran = vi.fn(() => {
      throw new Error("plugin bug");
    });
    live.register({ id: "p:x", area: "keybind", source: "plugin:p", keys: "Mod+Alt+J", run: ran });
    function Probe() {
      useContributedKeybinds(LINUX, live);
      return null;
    }
    render(createElement(Probe));
    const event = key({ key: "j", ctrlKey: true, altKey: true });
    act(() => {
      window.dispatchEvent(event);
    });
    expect(ran).toHaveBeenCalledTimes(1);
    expect(event.defaultPrevented).toBe(true);
    expect(warn).toHaveBeenCalled();
    act(() => live.removeSource("plugin:p"));
    act(() => {
      window.dispatchEvent(key({ key: "j", ctrlKey: true, altKey: true }));
    });
    expect(ran).toHaveBeenCalledTimes(1);
  });
});

describe("palette-command area", () => {
  it("opens on Ctrl+Shift+P, filters, runs the chosen command and closes", () => {
    const registry = new ContributionRegistry();
    const run = vi.fn();
    const home = vi.fn();
    registry.register({
      id: "p:deploy",
      area: "palette-command",
      source: "plugin:p",
      title: "Deploy preview",
      hint: "staging",
      run,
    });
    render(
      createElement(CommandPalette, {
        builtIn: [{ id: "app.home", title: "Go to Home", run: home }],
        registry,
      }),
    );
    expect(screen.queryByRole("dialog")).toBeNull();
    act(() => {
      window.dispatchEvent(key({ key: "P", ctrlKey: true, shiftKey: true }));
    });
    expect(screen.getAllByRole("option").map((o) => o.textContent)).toEqual([
      "Go to Home",
      "Deploy previewstaging",
    ]);
    const input = screen.getByRole("combobox");
    fireEvent.change(input, { target: { value: "stag" } });
    expect(screen.getAllByRole("option")).toHaveLength(1);
    fireEvent.keyDown(input, { key: "Enter" });
    expect(run).toHaveBeenCalledTimes(1);
    expect(home).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).toBeNull();
    act(() => {
      window.dispatchEvent(key({ key: "P", ctrlKey: true, shiftKey: true }));
    });
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "nothing like it" } });
    expect(screen.queryAllByRole("option")).toHaveLength(0);
    fireEvent.keyDown(screen.getByRole("combobox"), { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

describe("theme area", () => {
  it("offers checked themes as appearance choices and withdraws them with their plugin", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const registry = new ContributionRegistry();
    const store = new AppearanceStore(null);
    const stop = mirrorThemeContributions(registry, store);
    registry.register({
      id: "p:dusk",
      area: "theme",
      source: "plugin:p",
      label: "Dusk",
      tokens: { light: colours("#fafafa"), dark: colours("#101820") },
    });
    registry.register({
      id: "p:flat",
      area: "theme",
      source: "plugin:p",
      label: "Flat",
      tokens: colours("#334455"),
    });
    // CSS text where a colour belongs: refused, never painted
    registry.register({
      id: "q:sneaky",
      area: "theme",
      source: "plugin:q",
      label: "Sneaky",
      tokens: { ...colours("#000000"), bg: "red; } body { display: none" },
    });
    expect(store.contributed().map((t) => t.id)).toEqual(["p:dusk", "p:flat"]);
    expect(warn).toHaveBeenCalled();

    store.choose("contrib:p:dusk");
    expect(store.resolve(null, "dark")?.bg.hex).toBe("#101820");
    expect(store.resolve(null, "light")?.bg.hex).toBe("#fafafa");
    expect(store.css(null, "dark")).toContain("--indus-color-bg: #101820;");
    store.choose("contrib:p:flat");
    expect(store.resolve(null, "light")?.fg.hex).toBe("#334455");

    registry.removeSource("plugin:p");
    expect(store.contributed()).toEqual([]);
    // the choice stays, painting the built-in look until the plugin is back
    expect(store.get().theme).toBe("contrib:p:flat");
    expect(store.resolve(null, "dark")).toBeNull();
    stop();
  });
});
