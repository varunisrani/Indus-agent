import { act, screen, waitFor, within } from "@testing-library/react";
import { createElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createMockApi, type MockApi } from "../testing/mockApi.ts";
import { currentLocation, renderApp } from "../testing/render.tsx";
import { type PluginManifest, pluginRegistry } from "./registry.ts";

let mock: MockApi;
let restore: () => void;

const HELLO: PluginManifest = {
  name: "hello",
  label: "Hello",
  tab: { path: "/hello", position: "after:skills" },
  entry: "dist/index.js",
  slots: ["sessions:top"],
};

beforeEach(() => {
  mock = createMockApi();
  restore = mock.install();
  vi.spyOn(console, "warn").mockImplementation(() => {});
});

afterEach(() => {
  restore();
  pluginRegistry.clear();
  for (const el of document.querySelectorAll("[data-indus-plugin]")) el.remove();
});

function listPlugins(plugins: PluginManifest[]) {
  // the list is asked once per shell mount; queue a few answers
  for (let i = 0; i < 4; i += 1) {
    mock.failNext("GET", "/api/dashboard/plugins", { status: 200, body: { plugins } });
  }
}

describe("plugins in the shell", () => {
  it("a deep link to a plugin tab waits for the plugin instead of falling back", async () => {
    listPlugins([HELLO]);
    renderApp("/hello");
    await waitFor(() =>
      expect(document.querySelector('script[data-indus-plugin="hello"]')).not.toBeNull(),
    );
    expect(currentLocation()).toBe("/hello");
    act(() => pluginRegistry.register("hello", () => createElement("p", null, "hello tab")));
    expect(await screen.findByText("hello tab")).toBeTruthy();
    const nav = within(screen.getByRole("navigation", { name: "Dashboard" }));
    const links = nav.getAllByRole("link").map((a) => a.textContent);
    expect(links.indexOf("Hello")).toBe(links.indexOf("Skills") + 1);
  });

  it("page slots render around their page, also when registered late", async () => {
    listPlugins([HELLO]);
    renderApp("/sessions");
    await waitFor(() => expect(currentLocation()).toBe("/sessions"));
    act(() =>
      pluginRegistry.registerSlot("hello", "sessions:top", () =>
        createElement("p", null, "slot above sessions"),
      ),
    );
    expect(await screen.findByText("slot above sessions")).toBeTruthy();
  });

  it("no plugin list (older backend) leaves the shell as it was", async () => {
    renderApp("/no/such/page");
    await waitFor(() => expect(currentLocation()).toBe("/sessions"));
  });
});
