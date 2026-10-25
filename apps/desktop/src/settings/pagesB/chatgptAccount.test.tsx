// @vitest-environment jsdom
// Settings › Providers › Accounts: the ChatGPT sign-in card (W61-M2) against a fake of main's REST
// proxy speaking M1's contract — sign in with the device flow inside the card, the account with
// plan and expiry, sign out after asking; never anything but metadata over the bridge.

import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { SettingsContext, type SettingsContextValue } from "../context.tsx";
import { type FakeRest, fakeRest } from "../fakeRest.ts";
import { ChatGptAccount, MAX_POLL_FAILURES, type Timers } from "./chatgptAccount.tsx";
import { AccountsPage } from "./providers.tsx";

const ACCOUNTS = "/api/providers/login/accounts/openai-codex";
const code = () => ["M3VB", "2XQW"].join("-");

function manualTimers() {
  const queue: Array<{ fn: () => void; id: number; ms: number }> = [];
  let next = 1;
  const timers: Timers = {
    setTimeout: (fn, ms) => {
      const id = next++;
      queue.push({ fn, id, ms });
      return id;
    },
    clearTimeout: (handle) => {
      const index = queue.findIndex((item) => item.id === handle);
      if (index >= 0) queue.splice(index, 1);
    },
  };
  return {
    timers,
    pending: () => queue.map((item) => item.ms),
    async flush() {
      const items = queue.splice(0);
      await act(async () => {
        for (const item of items) item.fn();
        await new Promise((resolve) => setTimeout(resolve, 0));
      });
    },
  };
}

async function settle() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

function signIns(signedIn: boolean, extra: Record<string, unknown> = {}) {
  return {
    provider: "openai-codex",
    label: "OpenAI (ChatGPT sign-in)",
    signed_in: signedIn,
    signed_in_at: signedIn ? "2026-10-25T09:00:00+00:00" : null,
    flows: { browser: true, device: true },
    accounts: signedIn
      ? [
          {
            id: "acct-1",
            label: "device_code-1",
            source: "device_code",
            status: "ok",
            expires_at: new Date(Date.now() + 3 * 86_400_000).toISOString(),
            expired: false,
            plan: "plus",
            ...extra,
          },
        ]
      : [],
  };
}

function context(
  api: FakeRest,
  overrides: Partial<SettingsContextValue> = {},
): SettingsContextValue {
  return {
    api,
    bridge: null,
    profile: "work",
    activeProfile: "work",
    saveKey: null,
    openOnboarding: () => undefined,
    config: null,
    reloadConfig: () => undefined,
    focusRow: null,
    ...overrides,
  };
}

function backend(signedIn: boolean) {
  const api = fakeRest();
  let state = signIns(signedIn);
  api.route("GET", ACCOUNTS, () => ({ status: 200, body: state }));
  api.route("DELETE", ACCOUNTS, () => {
    const removed = state.accounts.length;
    state = signIns(false);
    return { status: 200, body: { provider: "openai-codex", removed, signed_in: false } };
  });
  api.route("POST", "/api/providers/login/start", {
    session_id: "login-1",
    provider: "openai-codex",
    status: "pending",
    message: "",
    user_code: code(),
    verification_url: "https://signin.example.org/codex/device",
    expires_in: 900,
    interval: 8,
  });
  let status = "pending";
  api.route("GET", "/api/providers/login/login-1", () => {
    if (status === "approved") state = signIns(true);
    return {
      status: 200,
      body: { session_id: "login-1", provider: "openai-codex", status, message: "" },
    };
  });
  api.route("DELETE", "/api/providers/login/login-1", {
    session_id: "login-1",
    provider: "openai-codex",
    status: "cancelled",
    message: "",
  });
  return {
    api,
    setStatus: (next: string) => {
      status = next;
    },
  };
}

function card() {
  return within(screen.getByRole("region", { name: "ChatGPT" }));
}

describe("ChatGPT account card", () => {
  it("signs in inside the card: code, copy, page, polls at the interval, then the account", async () => {
    const { api, setStatus } = backend(false);
    const clock = manualTimers();
    const copy = vi.fn(async () => undefined);
    const openExternal = vi.fn();
    const changed = vi.fn();
    render(
      <SettingsContext.Provider value={context(api)}>
        <ChatGptAccount
          timers={clock.timers}
          copy={copy}
          openExternal={openExternal}
          onChanged={changed}
        />
      </SettingsContext.Provider>,
    );
    await settle();
    expect(api.calls[0]).toMatchObject({ method: "GET", path: ACCOUNTS, profile: "work" });
    await act(async () => {
      fireEvent.click(card().getByRole("button", { name: "Sign in" }));
    });
    await settle();
    const start = api.calls.find((c) => c.path === "/api/providers/login/start");
    expect(start).toMatchObject({
      method: "POST",
      body: { provider: "openai-codex" },
      profile: "work",
    });
    expect(card().getByTitle("Sign-in code").textContent).toBe(code());
    await act(async () => {
      fireEvent.click(card().getByRole("button", { name: "Copy the code" }));
    });
    expect(copy).toHaveBeenCalledWith(code());
    expect(card().getByRole("button", { name: "Copied" })).toBeTruthy();
    fireEvent.click(card().getByRole("button", { name: "Open the sign-in page" }));
    expect(openExternal).toHaveBeenCalledWith("https://signin.example.org/codex/device");
    expect(clock.pending()).toEqual([8_000]);
    expect(card().getByRole("status").textContent).toContain("Waiting");

    await clock.flush();
    expect(clock.pending()).toEqual([8_000]);
    setStatus("approved");
    await clock.flush();
    await settle();
    expect(clock.pending()).toEqual([]);
    expect(card().getByRole("status").textContent).toContain("Signed in");
    expect(changed).toHaveBeenCalledTimes(1);
    const account = card().getByRole("listitem");
    expect(account.textContent).toContain("device_code-1");
    expect(account.textContent).toContain("Plus");
    expect(account.textContent).toMatch(/expires in (3 days|7\d h)/);
    expect(
      api.calls.filter((c) => c.method === "GET" && c.path === "/api/providers/login/login-1"),
    ).toHaveLength(2);
    expect(api.calls.some((c) => c.method === "DELETE")).toBe(false);
  });

  it("Cancel ends the session on the backend and stops polling", async () => {
    const { api } = backend(false);
    const clock = manualTimers();
    render(
      <SettingsContext.Provider value={context(api)}>
        <ChatGptAccount timers={clock.timers} />
      </SettingsContext.Provider>,
    );
    await settle();
    await act(async () => {
      fireEvent.click(card().getByRole("button", { name: "Sign in" }));
    });
    await settle();
    await act(async () => {
      fireEvent.click(card().getByRole("button", { name: "Cancel" }));
    });
    await settle();
    expect(clock.pending()).toEqual([]);
    expect(api.calls.filter((c) => c.method === "DELETE").map((c) => c.path)).toEqual([
      "/api/providers/login/login-1",
    ]);
    expect(card().getByRole("status").textContent).toContain("cancelled");
    fireEvent.click(card().getByRole("button", { name: "Close" }));
    expect(card().getByRole("button", { name: "Sign in" })).toBeTruthy();
  });

  it.each([
    ["denied", "refused"],
    ["expired", "expired"],
    ["failed", "did not finish"],
  ])("a %s sign-in is said plainly, with Try again", async (end, words) => {
    const { api, setStatus } = backend(false);
    const clock = manualTimers();
    render(
      <SettingsContext.Provider value={context(api)}>
        <ChatGptAccount timers={clock.timers} />
      </SettingsContext.Provider>,
    );
    await settle();
    await act(async () => {
      fireEvent.click(card().getByRole("button", { name: "Sign in" }));
    });
    await settle();
    setStatus(end);
    await clock.flush();
    const status = card().getByRole("status");
    expect(status.getAttribute("data-status")).toBe(end);
    expect(status.textContent).toContain(words);
    expect(clock.pending()).toEqual([]);
    await act(async () => {
      fireEvent.click(card().getByRole("button", { name: "Try again" }));
    });
    await settle();
    expect(api.calls.filter((c) => c.path === "/api/providers/login/start")).toHaveLength(2);
  });

  it("keeps polling through a few failed polls, then gives up", async () => {
    const { api } = backend(false);
    const clock = manualTimers();
    render(
      <SettingsContext.Provider value={context(api)}>
        <ChatGptAccount timers={clock.timers} />
      </SettingsContext.Provider>,
    );
    await settle();
    await act(async () => {
      fireEvent.click(card().getByRole("button", { name: "Sign in" }));
    });
    await settle();
    api.route("GET", "/api/providers/login/login-1", () => ({
      status: 503,
      body: { detail: "down" },
    }));
    for (let n = 1; n < MAX_POLL_FAILURES; n += 1) {
      await clock.flush();
      expect(clock.pending()).toEqual([8_000]);
    }
    await clock.flush();
    expect(clock.pending()).toEqual([]);
    expect(card().getByRole("status").getAttribute("data-status")).toBe("failed");
  });

  it("shows why a code could not be had", async () => {
    const { api } = backend(false);
    api.route("POST", "/api/providers/login/start", () => ({
      status: 502,
      body: {
        detail: { code: "sign_in_unavailable", message: "The sign-in service did not answer." },
      },
    }));
    render(
      <SettingsContext.Provider value={context(api)}>
        <ChatGptAccount timers={manualTimers().timers} />
      </SettingsContext.Provider>,
    );
    await settle();
    await act(async () => {
      fireEvent.click(card().getByRole("button", { name: "Sign in" }));
    });
    await settle();
    expect(card().getByText("The sign-in service did not answer.")).toBeTruthy();
    expect(card().getByRole("button", { name: "Sign in" })).toBeTruthy();
  });

  it("lists the signed-in account and signs out only after asking", async () => {
    const { api } = backend(true);
    const changed = vi.fn();
    render(
      <SettingsContext.Provider value={context(api)}>
        <ChatGptAccount timers={manualTimers().timers} onChanged={changed} />
      </SettingsContext.Provider>,
    );
    await settle();
    expect(card().getByText("Signed in")).toBeTruthy();
    expect(card().queryByRole("button", { name: "Sign in" })).toBeNull();
    fireEvent.click(card().getByRole("button", { name: "Sign out" }));
    const confirm = within(card().getByRole("alertdialog", { name: "Sign out of ChatGPT" }));
    fireEvent.click(confirm.getByRole("button", { name: "Stay signed in" }));
    expect(card().queryByRole("alertdialog")).toBeNull();
    expect(api.calls.some((c) => c.method === "DELETE")).toBe(false);

    fireEvent.click(card().getByRole("button", { name: "Sign out" }));
    await act(async () => {
      fireEvent.click(
        within(card().getByRole("alertdialog")).getByRole("button", { name: "Sign out" }),
      );
    });
    await settle();
    expect(api.calls.filter((c) => c.method === "DELETE")).toMatchObject([
      { path: ACCOUNTS, profile: "work" },
    ]);
    expect(changed).toHaveBeenCalledTimes(1);
    expect(card().getByRole("button", { name: "Sign in" })).toBeTruthy();
    expect(card().queryByRole("listitem")).toBeNull();
  });

  it("says when the sign-in belongs to the default profile", async () => {
    const { api } = backend(true);
    api.route("DELETE", ACCOUNTS, () => ({
      status: 409,
      body: {
        detail: {
          code: "inherited_pool",
          message: "These sign-ins belong to the default profile.",
        },
      },
    }));
    render(
      <SettingsContext.Provider value={context(api)}>
        <ChatGptAccount timers={manualTimers().timers} />
      </SettingsContext.Provider>,
    );
    await settle();
    fireEvent.click(card().getByRole("button", { name: "Sign out" }));
    await act(async () => {
      fireEvent.click(
        within(card().getByRole("alertdialog")).getByRole("button", { name: "Sign out" }),
      );
    });
    await settle();
    expect(card().getByText("These sign-ins belong to the default profile.")).toBeTruthy();
    expect(card().getByRole("listitem")).toBeTruthy();
  });

  it("an expired access says it is renewed on the next use", async () => {
    const api = fakeRest();
    api.route(
      "GET",
      ACCOUNTS,
      signIns(true, {
        expired: true,
        plan: null,
        expires_at: new Date(Date.now() - 2 * 3_600_000).toISOString(),
      }),
    );
    render(
      <SettingsContext.Provider value={context(api)}>
        <ChatGptAccount timers={manualTimers().timers} />
      </SettingsContext.Provider>,
    );
    await settle();
    const account = card().getByRole("listitem");
    expect(account.textContent).toContain("Expired");
    expect(account.textContent).toContain("2 h ago");
    expect(account.textContent).toContain("renewed on the next use");
    expect(account.textContent).not.toContain("Plus");
  });
});

describe("Providers › Accounts", () => {
  it("lists the ChatGPT card instead of a generic sign-in row for it", async () => {
    const { api } = backend(false);
    api.route("GET", "/api/model/options", {
      providers: [
        {
          name: "openai-codex",
          label: "OpenAI (ChatGPT sign-in)",
          auth_type: "oauth",
          configured: false,
          device_login: true,
        },
        {
          name: "acme",
          label: "Acme Cloud",
          auth_type: "oauth",
          configured: false,
          device_login: true,
        },
      ],
      current: { provider: null, model: null },
    });
    render(
      <SettingsContext.Provider value={context(api)}>
        <AccountsPage />
      </SettingsContext.Provider>,
    );
    await settle();
    await settle();
    expect(screen.getByRole("region", { name: "ChatGPT" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Acme Cloud" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "OpenAI (ChatGPT sign-in)" })).toBeNull();
  });

  it("keeps the provider setup card beside the ChatGPT card", async () => {
    const { api } = backend(false);
    api.route("GET", "/api/model/options", {
      providers: [
        {
          name: "openai-codex",
          label: "OpenAI (ChatGPT sign-in)",
          auth_type: "oauth",
          configured: false,
          device_login: true,
        },
      ],
      current: { provider: null, model: null },
    });
    render(
      <SettingsContext.Provider value={context(api)}>
        <AccountsPage />
      </SettingsContext.Provider>,
    );
    await settle();
    await settle();
    expect(screen.getByRole("region", { name: "ChatGPT" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Set up a provider" })).toBeTruthy();
  });

  it("has no ChatGPT card when the backend does not offer the provider", async () => {
    const api = fakeRest();
    api.route("GET", "/api/model/options", {
      providers: [
        {
          name: "openrouter",
          label: "OpenRouter",
          auth_type: "api_key",
          configured: false,
          device_login: false,
        },
      ],
      current: { provider: null, model: null },
    });
    render(
      <SettingsContext.Provider value={context(api)}>
        <AccountsPage />
      </SettingsContext.Provider>,
    );
    await settle();
    expect(screen.queryByRole("region", { name: "ChatGPT" })).toBeNull();
    expect(api.calls.some((c) => c.path === ACCOUNTS)).toBe(false);
  });
});
