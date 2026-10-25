import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import axe from "axe-core";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DOCS_LINK, ROUTES } from "./shell/routes.ts";
import { SIDEBAR_KEY } from "./shell/Sidebar.tsx";
import { UNREACHABLE_TOAST } from "./shell/Toasts.tsx";
import { createMockApi, type MockApi } from "./testing/mockApi.ts";
import { currentLocation } from "./testing/render.tsx";
import { renderApp } from "./testing/renderApp.tsx";

let mock: MockApi;
let restore: () => void;

beforeEach(() => {
  mock = createMockApi();
  restore = mock.install();
  vi.spyOn(console, "warn").mockImplementation(() => {});
});

afterEach(() => {
  restore();
});

function heading(): string {
  return screen.getByRole("heading", { level: 1 }).textContent ?? "";
}

function nav() {
  return within(screen.getByRole("navigation", { name: "Dashboard" }));
}

describe("routes", () => {
  it("redirects the root and unknown paths to sessions", async () => {
    const first = renderApp("/");
    await waitFor(() => expect(currentLocation()).toBe("/sessions"));
    await waitFor(() => expect(heading()).toBe("Sessions"));
    first.unmount();

    renderApp("/no/such/page");
    await waitFor(() => expect(currentLocation()).toBe("/sessions"));
  });

  it("keeps the profile when redirecting", async () => {
    renderApp("/nowhere?profile=work");
    await waitFor(() => expect(currentLocation()).toBe("/sessions?profile=work"));
  });

  // One test per route: each mounts the whole shell, which is real render work in jsdom (about
  // 0.1-0.4 s idle), so one test for all 19 routes outgrew the 5 s budget on a busy machine.
  it.each(ROUTES.map((route) => [route.path, route] as const))(
    "resolves the route %s",
    async (_path, route) => {
      renderApp(route.path);
      await waitFor(() => expect(currentLocation()).toBe(route.path));
      await waitFor(() => expect(heading()).toBe(route.title));
    },
  );

  it("lists Documentation as an in-app page whose panel links the docs out", async () => {
    renderApp("/sessions");
    const item = nav().getByRole("link", { name: "Documentation" });
    expect(item.getAttribute("target")).toBeNull();
    fireEvent.click(item);
    await waitFor(() => expect(currentLocation()).toBe("/docs"));
    const open = await screen.findByRole("link", { name: "Open docs" });
    expect(open.getAttribute("target")).toBe("_blank");
    expect(open.getAttribute("rel")).toContain("noopener");
    expect(open.getAttribute("href")).toBe(DOCS_LINK.href);
  });

  it("lists Analytics only when the config flag is on", async () => {
    renderApp("/sessions");
    await screen.findByRole("link", { name: "Logs" });
    expect(nav().queryByRole("link", { name: "Analytics" })).toBeNull();

    mock.state.config = { dashboard: { show_token_analytics: true } };
    renderApp("/sessions");
    await waitFor(() =>
      expect(screen.getAllByRole("link", { name: "Analytics" }).length).toBeGreaterThan(0),
    );
  });

  it("drops the chat tab when the backend disables embedded chat", async () => {
    window.__INDUS_DASHBOARD_EMBEDDED_CHAT__ = false;
    renderApp("/chat");
    await waitFor(() => expect(currentLocation()).toBe("/sessions"));
    expect(nav().queryByRole("link", { name: "Chat" })).toBeNull();
  });
});

describe("persistent chat host", () => {
  it("does not mount the chat host before the first visit", async () => {
    renderApp("/sessions");
    await screen.findByRole("link", { name: "Chat" });
    expect(screen.queryByTestId("chat-host")).toBeNull();
  });

  it("keeps the same chat host node across tab switches", async () => {
    renderApp("/chat");
    const host = await screen.findByTestId("chat-host");
    expect(host.getAttribute("data-chat-active")).toBe("true");
    expect(host.getAttribute("aria-hidden")).toBeNull();

    fireEvent.click(nav().getByRole("link", { name: "Logs" }));
    await waitFor(() => expect(currentLocation()).toBe("/logs"));
    const hidden = screen.getByTestId("chat-host");
    expect(hidden).toBe(host);
    expect(hidden.style.display).toBe("none");
    expect(hidden.getAttribute("aria-hidden")).toBe("true");

    fireEvent.click(nav().getByRole("link", { name: "Chat" }));
    await waitFor(() => expect(currentLocation()).toBe("/chat"));
    expect(screen.getByTestId("chat-host")).toBe(host);
    expect(host.style.display).toBe("");
  });

  it("mounts the host on a later first visit", async () => {
    renderApp("/logs");
    await screen.findByRole("link", { name: "Chat" });
    fireEvent.click(nav().getByRole("link", { name: "Chat" }));
    expect(await screen.findByTestId("chat-host")).toBeTruthy();
  });
});

describe("sidebar", () => {
  it("remembers the collapsed sidebar", async () => {
    const first = renderApp("/sessions");
    fireEvent.click(screen.getByRole("button", { name: "Collapse sidebar" }));
    expect(window.localStorage.getItem(SIDEBAR_KEY)).toBe("true");
    expect(screen.getByTestId("sidebar").getAttribute("data-collapsed")).toBe("true");
    first.unmount();

    renderApp("/sessions");
    expect(screen.getByTestId("sidebar").getAttribute("data-collapsed")).toBe("true");
    fireEvent.click(screen.getByRole("button", { name: "Expand sidebar" }));
    expect(window.localStorage.getItem(SIDEBAR_KEY)).toBe("false");
  });

  it("becomes a slide-over below 1,024 px", async () => {
    const listeners: Array<() => void> = [];
    vi.stubGlobal(
      "matchMedia",
      vi.fn((query: string) => ({
        matches: false,
        media: query,
        addEventListener: (_: string, cb: () => void) => listeners.push(cb),
        removeEventListener: () => {},
      })),
    );
    try {
      renderApp("/sessions");
      expect(screen.queryByTestId("sidebar")).toBeNull();
      fireEvent.click(screen.getByRole("button", { name: "Open navigation" }));
      expect(screen.getByTestId("sidebar-drawer")).toBeTruthy();
      fireEvent.click(nav().getByRole("link", { name: "Logs" }));
      await waitFor(() => expect(currentLocation()).toBe("/logs"));
      expect(screen.queryByTestId("sidebar-drawer")).toBeNull();

      fireEvent.click(screen.getByRole("button", { name: "Open navigation" }));
      fireEvent.keyDown(window, { key: "Escape" });
      expect(screen.queryByTestId("sidebar-drawer")).toBeNull();
    } finally {
      vi.unstubAllGlobals();
    }
  });
});

describe("profiles", () => {
  it("hides the switcher with one profile", async () => {
    renderApp("/sessions");
    await waitFor(() => expect(mock.callsTo("GET", "/api/profiles").length).toBeGreaterThan(0));
    expect(screen.queryByRole("combobox", { name: "Profile" })).toBeNull();
  });

  it("shows the switcher and banner for a non-default profile", async () => {
    mock.state.profiles = {
      profiles: [
        { name: "default", is_default: true },
        { name: "work", is_default: false },
      ],
      active: "default",
    };
    renderApp("/logs?profile=work");
    const select = await screen.findByRole("combobox", { name: "Profile" });
    expect((select as HTMLSelectElement).value).toBe("work");
    expect(screen.getByText(/Managing profile/).textContent).toContain("work");
    await waitFor(() => expect(mock.callsTo("GET", "/api/logs").length).toBeGreaterThan(0));
    expect(mock.callsTo("GET", "/api/logs")[0]?.query.get("profile")).toBe("work");

    // Nav links keep the scope.
    expect(nav().getByRole("link", { name: "Sessions" }).getAttribute("href")).toBe(
      "/sessions?profile=work",
    );

    // Switching back refetches under the default profile with a fresh page tree.
    const before = mock.callsTo("GET", "/api/logs").length;
    fireEvent.change(select, { target: { value: "default" } });
    await waitFor(() => expect(currentLocation()).toBe("/logs?profile=default"));
    await waitFor(() => expect(mock.callsTo("GET", "/api/logs").length).toBeGreaterThan(before));
    expect(mock.callsTo("GET", "/api/logs").at(-1)?.query.get("profile")).toBe("default");
    expect(screen.queryByText(/Managing profile/)).toBeNull();
  });
});

describe("failures and accessibility", () => {
  it("shows one plain toast when the backend is unreachable", async () => {
    globalThis.fetch = vi.fn(() =>
      Promise.reject(new TypeError("Failed to fetch")),
    ) as typeof fetch;
    renderApp("/sessions");
    const toast = await screen.findByText(UNREACHABLE_TOAST);
    expect(toast).toBeTruthy();
    expect(screen.getAllByText(UNREACHABLE_TOAST)).toHaveLength(1);
    expect(screen.queryByText(/Failed to fetch/)).toBeNull();
  });

  it("has no axe violations on the shell", async () => {
    const { container } = renderApp("/sessions");
    await screen.findByRole("list", { name: "Recent sessions" });
    let violations: axe.Result[] = [];
    await act(async () => {
      const results = await axe.run(container, {
        runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"] },
        rules: { "color-contrast": { enabled: false } },
      });
      violations = results.violations;
    });
    expect(
      violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(", ")}`),
    ).toEqual([]);
  });
});
