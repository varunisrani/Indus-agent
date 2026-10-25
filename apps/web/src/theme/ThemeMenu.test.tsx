import { BRAND_NAME, BRAND_WORDMARK, MARK_PATH } from "@indus/ui";
import { fireEvent, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createMockApi, type MockApi } from "../testing/mockApi.ts";
import { renderApp } from "../testing/renderApp.tsx";
import { THEME_KEY } from "./ThemeProvider.tsx";

let mock: MockApi;
let restore: () => void;

beforeEach(() => {
  window.localStorage.clear();
  mock = createMockApi();
  restore = mock.install();
  vi.spyOn(console, "warn").mockImplementation(() => {});
});

afterEach(() => {
  restore();
  window.localStorage.clear();
});

describe("theme menu", () => {
  it("lists the presets and applies the one picked", async () => {
    window.localStorage.setItem(THEME_KEY, "deep-teal");
    renderApp("/sessions");
    expect(document.documentElement.dataset.preset).toBe("deep-teal");
    expect(document.documentElement.dataset.theme).toBe("indus-dark");

    fireEvent.click(await screen.findByRole("button", { name: "Theme: Deep Teal" }));
    const menu = screen.getByRole("menu", { name: "Theme" });
    const items = within(menu).getAllByRole("menuitemradio");
    expect(items.length).toBeGreaterThanOrEqual(2);
    expect(
      within(menu)
        .getByRole("menuitemradio", { name: /^Deep Teal Dark teal/ })
        .getAttribute("aria-checked"),
    ).toBe("true");

    fireEvent.click(within(menu).getByRole("menuitemradio", { name: /Clear Blue/ }));
    expect(screen.queryByRole("menu")).toBeNull();
    expect(document.documentElement.dataset.preset).toBe("clear-blue");
    expect(document.documentElement.dataset.theme).toBe("indus-light");
    expect(window.localStorage.getItem(THEME_KEY)).toBe("clear-blue");
  });

  it("starts on Indus Ember and offers Indus Daylight (W63)", async () => {
    renderApp("/sessions");
    expect(document.documentElement.dataset.preset).toBe("indus-ember");
    expect(document.documentElement.dataset.theme).toBe("indus-dark");
    fireEvent.click(await screen.findByRole("button", { name: "Theme: Indus Ember" }));
    const menu = screen.getByRole("menu", { name: "Theme" });
    expect(
      within(menu)
        .getByRole("menuitemradio", { name: /^Indus Ember The Indus AGI lockup/ })
        .getAttribute("aria-checked"),
    ).toBe("true");
    expect(within(menu).getByRole("menuitemradio", { name: /^Indus Ember Large/ })).toBeTruthy();
    fireEvent.click(within(menu).getByRole("menuitemradio", { name: /^Indus Daylight/ }));
    expect(document.documentElement.dataset.preset).toBe("indus-daylight");
    expect(document.documentElement.dataset.theme).toBe("indus-light");
    expect(window.localStorage.getItem(THEME_KEY)).toBe("indus-daylight");
  });

  it("closes on Escape", async () => {
    renderApp("/sessions");
    fireEvent.click(await screen.findByRole("button", { name: /^Theme: / }));
    expect(screen.getByRole("menu")).toBeTruthy();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("keeps names and tooltips when the sidebar is collapsed", async () => {
    renderApp("/sessions");
    fireEvent.click(await screen.findByRole("button", { name: "Collapse sidebar" }));
    const nav = within(screen.getByRole("navigation", { name: "Dashboard" }));
    const logs = nav.getByRole("link", { name: "Logs" });
    // No clipped two-letter stubs: the collapsed link shows only its icon.
    expect(logs.textContent).toBe("");
    fireEvent.mouseEnter(logs);
    expect(screen.getByText("Logs", { selector: "span" })).toBeTruthy();
    expect(screen.getByRole("button", { name: /^Theme: / })).toBeTruthy();
  });
});

describe("sidebar brand (W63)", () => {
  it("shows the mark beside the two-line wordmark, and the mark alone when collapsed", async () => {
    renderApp("/sessions");
    const nav = within(screen.getByRole("navigation", { name: "Dashboard" }));
    const wordmark = await nav.findByTestId("wordmark");
    expect(Array.from(wordmark.querySelectorAll(".block"), (line) => line.textContent)).toEqual([
      ...BRAND_WORDMARK,
    ]);
    expect(wordmark.closest("[title]")?.getAttribute("title")).toBe(BRAND_NAME);
    const mark = nav.getByTestId("brand-mark");
    expect(mark.querySelector("path")?.getAttribute("d")).toBe(MARK_PATH);
    expect(mark.getAttribute("class")).toContain("fill-accent");
    expect(mark.getAttribute("aria-hidden")).toBe("true");

    fireEvent.click(nav.getByRole("button", { name: "Collapse sidebar" }));
    expect(nav.queryByTestId("wordmark")).toBeNull();
    const expand = nav.getByRole("button", { name: "Expand sidebar" });
    expect(within(expand).getByTestId("brand-mark")).toBeTruthy();
  });
});

describe("language menu", () => {
  it("offers English only, checked", async () => {
    renderApp("/sessions");
    fireEvent.click(await screen.findByRole("button", { name: "Language: English" }));
    const menu = screen.getByRole("menu", { name: "Language" });
    const items = within(menu).getAllByRole("menuitemradio");
    expect(items).toHaveLength(1);
    expect(items[0]?.getAttribute("aria-checked")).toBe("true");
  });
});
