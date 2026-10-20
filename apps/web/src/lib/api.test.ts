import { afterEach, beforeEach, describe, expect, it, type Mock, vi } from "vitest";
import { GENERATED_ROUTES } from "../test/_stubs_f1.ts";
import { createMockApi, type MockApi } from "../testing/mockApi.ts";
import {
  ApiError,
  api,
  isProfileScoped,
  LAST_LOCATION_KEY,
  onUnreachable,
  PROFILE_SCOPED_FAMILIES,
  rawRequest,
  request,
  SESSION_HEADER,
  setBrowserNavigation,
  setSelectedProfile,
  TOKEN_RELOAD_KEY,
  withProfile,
} from "./api.ts";
import {
  buildWsUrl,
  fetchWsTicket,
  TICKET_TIMEOUT_MS,
  WS_PROTOCOL,
  wsProtocols,
  wsTarget,
} from "./wsUrl.ts";

// Built at runtime so no credential-shaped literal sits in the source.
const fakeToken = () => ["tok", "en", "-", "for", "-", "tests"].join("");

let mock: MockApi;
let restoreFetch: () => void;
let navigation: { assign: Mock<(url: string) => void>; reload: Mock<() => void> };
let restoreNavigation: () => void;

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

/** Resolve or report that the promise is still pending after the microtask queue drains. */
async function settle<T>(
  promise: Promise<T>,
): Promise<
  { state: "pending" } | { state: "done"; value: T } | { state: "failed"; error: unknown }
> {
  let outcome:
    | { state: "pending" }
    | { state: "done"; value: T }
    | { state: "failed"; error: unknown } = { state: "pending" };
  promise.then(
    (value) => {
      outcome = { state: "done", value };
    },
    (error: unknown) => {
      outcome = { state: "failed", error };
    },
  );
  for (let i = 0; i < 20; i++) await Promise.resolve();
  await new Promise((resolve) => setTimeout(resolve, 0));
  return outcome;
}

describe("loopback mode", () => {
  it("sends the session token header and the base path", async () => {
    window.__INDUS_SESSION_TOKEN__ = fakeToken();
    window.__INDUS_BASE_PATH__ = "/indus/";
    await api.config.schema();
    const call = mock.calls.at(-1);
    expect(call?.headers.get(SESSION_HEADER)).toBe(fakeToken());
    expect(call?.url.startsWith("/indus/api/config/schema")).toBe(true);
  });

  it("reloads once on a loopback 401, then surfaces the error", async () => {
    window.__INDUS_SESSION_TOKEN__ = fakeToken();
    mock.failNext("GET", "/api/env", { status: 401, body: { detail: "bad token" } });
    const first = await settle(api.env.list());
    expect(first.state).toBe("pending");
    expect(navigation.reload).toHaveBeenCalledTimes(1);
    expect(window.sessionStorage.getItem(TOKEN_RELOAD_KEY)).not.toBeNull();

    // After the reload the token is still stale: no second reload, a plain error instead.
    mock.failNext("GET", "/api/env", { status: 401, body: { detail: "bad token" } });
    const second = await settle(api.env.list());
    expect(second.state).toBe("failed");
    expect(
      second.state === "failed" && second.error instanceof ApiError && second.error.status,
    ).toBe(401);
    expect(navigation.reload).toHaveBeenCalledTimes(1);
  });

  it("lets a caller keep a 401 to itself", async () => {
    mock.failNext("GET", "/api/env", { status: 401, body: {} });
    const outcome = await settle(request("/api/env", { allowUnauthorized: true }));
    expect(outcome.state).toBe("failed");
    expect(navigation.reload).not.toHaveBeenCalled();
    expect(window.sessionStorage.getItem(TOKEN_RELOAD_KEY)).toBeNull();
  });

  it("re-arms the reload guard after a 2xx", async () => {
    window.sessionStorage.setItem(TOKEN_RELOAD_KEY, "1");
    await api.env.list();
    expect(window.sessionStorage.getItem(TOKEN_RELOAD_KEY)).toBeNull();
    mock.failNext("GET", "/api/env", { status: 401, body: {} });
    expect((await settle(api.env.list())).state).toBe("pending");
    expect(navigation.reload).toHaveBeenCalledTimes(1);
  });
});

describe("gated mode", () => {
  it("navigates to the login URL on a gated 401 and remembers where the user was", async () => {
    window.__INDUS_AUTH_REQUIRED__ = true;
    window.history.replaceState(null, "", "/sessions?profile=work");
    mock.failNext("GET", "/api/config", {
      status: 401,
      body: { error: "session_expired", login_url: "/login?next=%2Fsessions" },
    });
    const outcome = await settle(api.config.get());
    expect(outcome.state).toBe("pending");
    expect(navigation.assign).toHaveBeenCalledWith("/login?next=%2Fsessions");
    expect(window.sessionStorage.getItem(LAST_LOCATION_KEY)).toBe("/sessions?profile=work");
    expect(navigation.reload).not.toHaveBeenCalled();
  });

  it("never reloads in gated mode, and sends no token header", async () => {
    window.__INDUS_AUTH_REQUIRED__ = true;
    mock.failNext("GET", "/api/env", { status: 401, body: { detail: "not yours" } });
    const outcome = await settle(api.env.list());
    expect(outcome.state).toBe("failed");
    expect(navigation.reload).not.toHaveBeenCalled();
    expect(navigation.assign).not.toHaveBeenCalled();
    expect(mock.calls.at(-1)?.headers.has(SESSION_HEADER)).toBe(false);
  });
});

describe("profile scope", () => {
  it("covers every scoped route in the generated route list", () => {
    const uncovered = GENERATED_ROUTES.filter(
      (route) => route.scoped && !isProfileScoped(route.path),
    );
    expect(uncovered).toEqual([]);
    const overreach = GENERATED_ROUTES.filter(
      (route) => !route.scoped && isProfileScoped(route.path),
    );
    expect(overreach).toEqual([]);
  });

  it("scopes by whole path segments", () => {
    expect(isProfileScoped("/api/model/info")).toBe(true);
    expect(isProfileScoped("/api/models")).toBe(false);
    expect(isProfileScoped("/api/configuration")).toBe(false);
    expect(isProfileScoped("/api/config?x=1")).toBe(true);
    expect(PROFILE_SCOPED_FAMILIES).toContain("/api/logs");
  });

  it("appends the selected profile to scoped calls only", async () => {
    setSelectedProfile("work");
    await api.logs({ file: "agent", level: "ALL", component: "all", lines: 100 });
    mock.state.files["/srv"] = [];
    await api.files.list("/srv");
    expect(mock.callsTo("GET", "/api/logs")[0]?.query.get("profile")).toBe("work");
    expect(mock.callsTo("GET", "/api/files")[0]?.query.has("profile")).toBe(false);
  });

  it("leaves an explicit profile alone", () => {
    setSelectedProfile("work");
    expect(withProfile("/api/config?profile=home")).toBe("/api/config?profile=home");
    expect(withProfile("/api/config")).toBe("/api/config?profile=work");
    expect(withProfile("/api/logs?file=agent")).toBe("/api/logs?file=agent&profile=work");
  });

  it("falls back to the backend's own profile when nothing is selected", async () => {
    window.__INDUS_DASHBOARD_PROFILE__ = "launch";
    await api.env.list();
    expect(mock.callsTo("GET", "/api/env")[0]?.query.get("profile")).toBe("launch");
  });

  it("raw requests carry the token and the profile", async () => {
    window.__INDUS_SESSION_TOKEN__ = fakeToken();
    setSelectedProfile("work");
    const res = await rawRequest("/api/config/schema");
    expect(res.ok).toBe(true);
    const call = mock.calls.at(-1);
    expect(call?.headers.get(SESSION_HEADER)).toBe(fakeToken());
    expect(call?.query.get("profile")).toBe("work");
  });
});

describe("errors", () => {
  it("turns a network failure into status 0 and tells the listeners once", async () => {
    const heard = vi.fn();
    const stop = onUnreachable(heard);
    globalThis.fetch = vi.fn(() =>
      Promise.reject(new TypeError("Failed to fetch")),
    ) as typeof fetch;
    const outcome = await settle(request("/api/env"));
    stop();
    expect(outcome.state).toBe("failed");
    const error = outcome.state === "failed" ? outcome.error : null;
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(0);
    expect((error as ApiError).message).toMatch(/unreachable/i);
    expect((error as ApiError).details).toContain("Failed to fetch");
    expect(heard).toHaveBeenCalledTimes(1);
  });

  it("reads a structured 409 code", async () => {
    mock.state.deleteConflicts["s-chat-1"] = "session_busy";
    const outcome = await settle(api.sessions.remove("s-chat-1"));
    const error = outcome.state === "failed" ? (outcome.error as ApiError) : null;
    expect(error?.status).toBe(409);
    expect(error?.code).toBe("session_busy");
  });

  it("sends the snapshot identity with a delete", async () => {
    await api.sessions.remove("s-chat-2");
    const call = mock.callsTo("DELETE", "/api/sessions/s-chat-2")[0];
    expect(call?.query.get("expected_revision")).toBe("7");
    expect(call?.query.get("expected_generation")).toBe("2");
    expect(call?.query.get("request_id")).toBeTruthy();
  });
});

describe("WebSocket URLs", () => {
  it("uses the session token in loopback mode, in the subprotocols only", async () => {
    window.__INDUS_SESSION_TOKEN__ = fakeToken();
    const target = await wsTarget("/api/pty", "web-pty", { channel: "c1" });
    const parsed = new URL(target.url);
    expect(parsed.protocol).toBe("ws:");
    expect(parsed.pathname).toBe("/api/pty");
    expect(parsed.searchParams.get("channel")).toBe("c1");
    expect(target.url).not.toContain(fakeToken());
    for (const name of ["token", "ticket", "internal"]) {
      expect(parsed.searchParams.has(name)).toBe(false);
    }
    expect(target.protocols).toEqual([WS_PROTOCOL, `indus-dashboard-token.${fakeToken()}`]);
  });

  it("fetches a fresh ticket in gated mode", async () => {
    window.__INDUS_AUTH_REQUIRED__ = true;
    window.__INDUS_SESSION_TOKEN__ = fakeToken();
    const minted = ["t", "k", "1"].join("");
    globalThis.fetch = vi.fn(
      async () => new Response(JSON.stringify({ ticket: minted, ttl_seconds: 30 })),
    ) as typeof fetch;
    const target = await wsTarget("/api/events", "web-events");
    expect(target.protocols).toEqual([WS_PROTOCOL, `indus-dashboard-ticket.${minted}`]);
    expect(target.protocols.join(",")).not.toContain(fakeToken());
    expect(new URL(target.url).search).toBe("");
  });

  it("builds URLs that never carry a credential", () => {
    window.__INDUS_SESSION_TOKEN__ = fakeToken();
    const url = buildWsUrl("/api/ws", { profile: "work", empty: "" });
    expect(new URL(url).search).toBe("?profile=work");
    expect(url).not.toContain(fakeToken());
    expect(wsProtocols(["token", ""])).toEqual([WS_PROTOCOL]);
  });

  it("gives up on the ticket fetch at 8 s", async () => {
    vi.useFakeTimers();
    try {
      globalThis.fetch = vi.fn(
        (_input: RequestInfo | URL, init?: RequestInit) =>
          new Promise<Response>((_resolve, reject) => {
            init?.signal?.addEventListener("abort", () =>
              reject(new DOMException("aborted", "AbortError")),
            );
          }),
      ) as typeof fetch;
      let failure: unknown = null;
      fetchWsTicket("web-pty").catch((error: unknown) => {
        failure = error;
      });
      await vi.advanceTimersByTimeAsync(TICKET_TIMEOUT_MS - 1);
      expect(globalThis.fetch).toHaveBeenCalledTimes(1);
      expect(failure).toBeNull();
      await vi.advanceTimersByTimeAsync(1);
      expect(failure).toBeInstanceOf(ApiError);
      expect((failure as unknown as ApiError).status).toBe(0);
      expect((failure as unknown as ApiError).message).toMatch(/in time/);
    } finally {
      vi.useRealTimers();
    }
  });
});
