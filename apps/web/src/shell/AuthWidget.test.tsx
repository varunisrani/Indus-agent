import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, type Mock, vi } from "vitest";
import { LAST_LOCATION_KEY, setBrowserNavigation, TOKEN_RELOAD_KEY } from "../lib/api.ts";
import { shortUserId, takeLastLocation } from "../lib/auth.ts";
import { createMockApi, type MockApi, type MockReply } from "../testing/mockApi.ts";
import { currentLocation } from "../testing/render.tsx";
import { renderApp } from "../testing/renderApp.tsx";
import { AuthWidget } from "./AuthWidget.tsx";

let mock: MockApi;
let restoreFetch: () => void;
let navigation: { assign: Mock<(url: string) => void>; reload: Mock<() => void> };
let restoreNavigation: () => void;

const gatedIdentity = (overrides: Record<string, unknown> = {}) => ({
  user_id: "user-0123456789abcdef",
  email: "",
  display_name: "",
  org_id: "",
  provider: "oidc",
  expires_at: 1_900_000_000,
  ...overrides,
});

const providers: MockReply = {
  status: 200,
  body: {
    auth_required: true,
    providers: [
      { name: "basic", display_name: "Password", kind: "password", supports_password: true },
      { name: "oidc", display_name: "Company SSO", kind: "redirect", supports_password: false },
    ],
  },
};

function answerIdentity(reply: MockReply): void {
  mock.failNext("GET", "/api/auth/me", reply);
}

function renderWidget(path = "/sessions") {
  return renderApp(path, { sidebarFooter: <AuthWidget /> });
}

beforeEach(() => {
  mock = createMockApi();
  restoreFetch = mock.install();
  navigation = { assign: vi.fn<(url: string) => void>(), reload: vi.fn<() => void>() };
  restoreNavigation = setBrowserNavigation(navigation);
  vi.spyOn(console, "warn").mockImplementation(() => {});
});

afterEach(() => {
  restoreFetch();
  restoreNavigation();
});

describe("loopback mode", () => {
  it("renders nothing for the loopback identity", async () => {
    answerIdentity({ status: 200, body: gatedIdentity({ provider: "loopback" }) });
    renderWidget();
    await waitFor(() => expect(mock.callsTo("GET", "/api/auth/me")).toHaveLength(1));
    await screen.findByRole("navigation", { name: "Dashboard" });
    expect(screen.queryByRole("button", { name: "Sign out" })).toBeNull();
    expect(screen.queryByText(/via /)).toBeNull();
  });

  it("keeps a 401 to itself: nothing shown and no reload", async () => {
    answerIdentity({ status: 401, body: { error: "unauthenticated", detail: "token missing" } });
    renderWidget();
    await waitFor(() => expect(mock.callsTo("GET", "/api/auth/me")).toHaveLength(1));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(navigation.reload).not.toHaveBeenCalled();
    expect(window.sessionStorage.getItem(TOKEN_RELOAD_KEY)).toBeNull();
    expect(screen.queryByRole("button", { name: "Sign out" })).toBeNull();
  });
});

describe("gated mode", () => {
  beforeEach(() => {
    window.__INDUS_AUTH_REQUIRED__ = true;
  });

  it("shows the user id cut to 14 characters and the provider's name", async () => {
    answerIdentity({ status: 200, body: gatedIdentity() });
    mock.failNext("GET", "/api/auth/providers", providers);
    renderWidget();
    const id = await screen.findByText("user-012345678…");
    expect(id.getAttribute("title")).toBe("user-0123456789abcdef");
    expect(await screen.findByText("via Company SSO")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Sign out" })).toBeTruthy();
  });

  it("shows the display name above the id and falls back to the provider key", async () => {
    answerIdentity({
      status: 200,
      body: gatedIdentity({ user_id: "admin", display_name: "Asha", provider: "basic" }),
    });
    mock.failNext("GET", "/api/auth/providers", { status: 500, body: {} });
    renderWidget();
    expect(await screen.findByText("Asha")).toBeTruthy();
    expect(screen.getByText("admin")).toBeTruthy();
    expect(await screen.findByText("via basic")).toBeTruthy();
    expect(screen.getByRole("region", { name: "Signed in as Asha" })).toBeTruthy();
  });

  it("signs out: POST under the base path, forgets the last location, opens the login page", async () => {
    window.__INDUS_BASE_PATH__ = "/indus";
    window.sessionStorage.setItem(LAST_LOCATION_KEY, "/indus/logs");
    answerIdentity({ status: 200, body: gatedIdentity() });
    mock.failNext("GET", "/api/auth/providers", providers);
    mock.failNext("POST", /\/auth\/logout$/, { status: 200, body: {} });
    renderWidget("/sessions");
    fireEvent.click(await screen.findByRole("button", { name: "Sign out" }));
    await waitFor(() => expect(navigation.assign).toHaveBeenCalledWith("/indus/login"));
    const [call] = mock.callsTo("POST", /\/auth\/logout$/);
    expect(call?.url).toBe("/indus/auth/logout");
    expect(window.sessionStorage.getItem(LAST_LOCATION_KEY)).toBeNull();
  });

  it("stays put and says so when sign-out gets no answer", async () => {
    answerIdentity({ status: 200, body: gatedIdentity() });
    mock.failNext("GET", "/api/auth/providers", providers);
    renderWidget();
    const button = await screen.findByRole("button", { name: "Sign out" });
    const failing = vi.fn(() => Promise.reject(new TypeError("network down")));
    restoreFetch();
    restoreFetch = (() => {
      const previous = globalThis.fetch;
      globalThis.fetch = failing as unknown as typeof fetch;
      return () => {
        globalThis.fetch = previous;
      };
    })();
    fireEvent.click(button);
    const notice = await screen.findByText("Could not sign out");
    expect(notice.getAttribute("role")).toBe("alert");
    expect(screen.getByRole("button", { name: "Sign out" }).hasAttribute("disabled")).toBe(false);
    expect(navigation.assign).not.toHaveBeenCalled();
  });

  it("reports an identity it cannot read", async () => {
    answerIdentity({ status: 500, body: { detail: "boom" } });
    renderWidget();
    expect(await screen.findByText("Sign-in status unavailable")).toBeTruthy();
  });

  it("returns once to where the gated 401 sent the browser away from", async () => {
    window.sessionStorage.setItem(LAST_LOCATION_KEY, "/logs?file=errors");
    answerIdentity({ status: 200, body: gatedIdentity() });
    mock.failNext("GET", "/api/auth/providers", providers);
    renderWidget("/");
    await screen.findByRole("button", { name: "Sign out" });
    await waitFor(() => expect(currentLocation()).toBe("/logs?file=errors"));
    expect(window.sessionStorage.getItem(LAST_LOCATION_KEY)).toBeNull();
  });
});

describe("helpers", () => {
  it("cuts ids at 14 characters only when longer", () => {
    expect(shortUserId("short")).toBe("short");
    expect(shortUserId("exactly14chars")).toBe("exactly14chars");
    expect(shortUserId("fifteen-chars-x")).toBe("fifteen-chars-…");
  });

  it("accepts only in-app paths as the last location", () => {
    const take = (stored: string) => {
      window.sessionStorage.setItem(LAST_LOCATION_KEY, stored);
      return takeLastLocation();
    };
    expect(take("/sessions?tab=all")).toBe("/sessions?tab=all");
    expect(take("//evil.example/")).toBeNull();
    expect(take("https://evil.example/")).toBeNull();
    expect(take("/login?next=%2F")).toBeNull();
    window.__INDUS_BASE_PATH__ = "/indus";
    expect(take("/indus/cron")).toBe("/cron");
    expect(take("/indus")).toBe("/");
    expect(take("/other/cron")).toBeNull();
    expect(window.sessionStorage.getItem(LAST_LOCATION_KEY)).toBeNull();
  });
});
