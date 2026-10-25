// The ChatGPT sign-in on Keys › Provider logins (W61-M2), against the mock of M1's REST contract:
// Sign in starts the device flow at once, the code is shown large with a copy button and the
// verification link, the session is polled at the backend's interval and never faster, each end
// is said plainly, and a signed-in account is listed with Sign out (asks first).

import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../lib/api.ts";
import type { ProviderOption } from "../lib/restTypes.ts";
import { createMockApi, type MockApi } from "../testing/mockApi.ts";
import { renderPage } from "../testing/render.tsx";
import { EnvPage } from "./EnvPage.tsx";

let mock: MockApi;
let restore: () => void;
const writeText = vi.fn(async (_text: string) => undefined);

const CHATGPT: ProviderOption = {
  name: "openai-codex",
  label: "OpenAI (ChatGPT sign-in)",
  description: "",
  family: "openai",
  auth_type: "oauth",
  configured: false,
  device_login: true,
  sign_in: true,
  supports_model_listing: true,
};

// A code shaped like the ones the sign-in page asks for, built at runtime.
const userCode = () => ["QX7M", "9PLD"].join("-");

beforeEach(() => {
  mock = createMockApi();
  mock.state.modelOptions.providers.push({ ...CHATGPT });
  mock.state.deviceGrant = {
    user_code: userCode(),
    verification_url: "https://signin.example.org/codex/device",
    expires_in: 900,
    interval: 7,
  };
  restore = mock.install();
  writeText.mockClear();
  Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
  vi.spyOn(console, "warn").mockImplementation(() => {});
});

afterEach(() => {
  restore();
  vi.useRealTimers();
});

function tick(ms: number) {
  act(() => {
    vi.advanceTimersByTime(ms);
  });
}

const polls = () => mock.callsTo("GET", "/api/providers/login/login-1");

async function chatgptRow() {
  return within(await screen.findByTestId("oauth-openai-codex"));
}

async function signIn() {
  const row = await chatgptRow();
  fireEvent.click(await row.findByRole("button", { name: "Sign in" }));
  expect((await row.findByTestId("device-code")).textContent).toBe(userCode());
  return row;
}

describe("ChatGPT sign-in on the Keys page", () => {
  it("starts at once, shows the code with copy and link, polls at the interval, then lists the account", async () => {
    mock.state.loginPolls = ["pending", "approved"];
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    renderPage(<EnvPage />, "/env");
    const row = await signIn();
    expect(mock.callsTo("POST", "/api/providers/login/start")[0]?.body).toEqual({
      provider: "openai-codex",
    });
    expect(row.queryByRole("button", { name: "Get a sign-in code" })).toBeNull();

    const link = row.getByRole("link", { name: "https://signin.example.org/codex/device" });
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("rel")).toContain("noopener");
    fireEvent.click(row.getByRole("button", { name: "Copy the code" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(userCode()));

    tick(6_999);
    expect(polls()).toHaveLength(0);
    tick(1);
    await waitFor(() => expect(polls()).toHaveLength(1));
    expect(row.getByRole("status").textContent).toContain("Waiting");
    tick(7_000);
    await waitFor(() => expect(row.getByRole("status").textContent).toContain("Signed in"));
    tick(60_000);
    expect(polls()).toHaveLength(2);

    const account = await row.findByTestId("sign-in-account-acct-1");
    expect(account.textContent).toContain("device_code-1");
    expect(account.textContent).toContain("Plus");
    expect(account.textContent).toContain("expires in");
    expect(row.getByRole("button", { name: "Sign out" })).toBeTruthy();
    expect(row.queryByRole("button", { name: "Sign in" })).toBeNull();
    expect(row.getByText("Connected.")).toBeTruthy();
  });

  it("never polls faster than once a second, whatever the interval says", async () => {
    mock.state.deviceGrant = {
      ...(mock.state.deviceGrant as NonNullable<typeof mock.state.deviceGrant>),
      interval: 0,
    };
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    renderPage(<EnvPage />, "/env");
    await signIn();
    tick(999);
    expect(polls()).toHaveLength(0);
    tick(1);
    await waitFor(() => expect(polls()).toHaveLength(1));
  });

  it.each([
    ["denied", "refused"],
    ["expired", "expired"],
    ["failed", "failed"],
  ] as const)("says %s plainly and offers Try again", async (end, words) => {
    mock.state.loginPolls = [end];
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    renderPage(<EnvPage />, "/env");
    const row = await signIn();
    tick(7_000);
    await waitFor(() => expect(row.getByRole("status").getAttribute("data-status")).toBe(end));
    expect(row.getByRole("status").textContent).toContain(words);
    expect(row.queryByTestId("device-code")).toBeNull();
    tick(30_000);
    expect(polls()).toHaveLength(1);
    fireEvent.click(row.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(mock.callsTo("POST", "/api/providers/login/start")).toHaveLength(2));
  });

  it("Cancel ends the session on the backend and stops polling", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    renderPage(<EnvPage />, "/env");
    const row = await signIn();
    tick(7_000);
    await waitFor(() => expect(polls()).toHaveLength(1));
    fireEvent.click(row.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(row.getByRole("status").textContent).toContain("cancelled"));
    expect(mock.callsTo("DELETE", "/api/providers/login/login-1")).toHaveLength(1);
    tick(30_000);
    expect(polls()).toHaveLength(1);
    fireEvent.click(row.getByRole("button", { name: "Close" }));
    expect(await row.findByRole("button", { name: "Sign in" })).toBeTruthy();
  });

  it("shows why OpenAI could not be asked for a code", async () => {
    mock.failNext("POST", "/api/providers/login/start", {
      status: 502,
      body: {
        detail: { code: "sign_in_unavailable", message: "The sign-in service did not answer." },
      },
    });
    renderPage(<EnvPage />, "/env");
    const row = await chatgptRow();
    fireEvent.click(await row.findByRole("button", { name: "Sign in" }));
    expect((await row.findByRole("alert")).textContent).toContain("did not answer");
    expect(polls()).toHaveLength(0);
    fireEvent.click(row.getByRole("button", { name: "Try again" }));
    expect((await row.findByTestId("device-code")).textContent).toBe(userCode());
  });

  it("lists a signed-in account and signs out after asking", async () => {
    mock.state.modelOptions.providers = mock.state.modelOptions.providers.map((p) =>
      p.name === "openai-codex" ? { ...p, configured: true } : p,
    );
    mock.state.signIns["openai-codex"] = {
      provider: "openai-codex",
      label: CHATGPT.label,
      signed_in: true,
      signed_in_at: "2026-10-25T09:00:00+00:00",
      flows: { browser: true, device: true },
      accounts: [
        {
          id: "acct-a",
          label: "work",
          source: "pkce",
          status: "ok",
          expires_at: "2026-10-20T09:00:00+00:00",
          expired: true,
          plan: "pro",
        },
      ],
    };
    renderPage(<EnvPage />, "/env");
    const row = await chatgptRow();
    const account = await row.findByTestId("sign-in-account-acct-a");
    expect(account.textContent).toContain("Pro");
    expect(account.textContent).toContain("expired");
    expect(account.textContent).toContain("renewed on the next use");

    fireEvent.click(row.getByRole("button", { name: "Sign out" }));
    const dialog = within(await screen.findByRole("dialog"));
    fireEvent.click(dialog.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(mock.callsTo("DELETE", "/api/providers/login/accounts/openai-codex")).toHaveLength(0);

    fireEvent.click(row.getByRole("button", { name: "Sign out" }));
    const again = within(await screen.findByRole("dialog"));
    fireEvent.click(again.getByRole("button", { name: "Sign out" }));
    await waitFor(() =>
      expect(mock.callsTo("DELETE", "/api/providers/login/accounts/openai-codex")).toHaveLength(1),
    );
    expect(await row.findByRole("button", { name: "Sign in" })).toBeTruthy();
    expect(row.queryByTestId("sign-in-account-acct-a")).toBeNull();
    expect(mock.state.signIns["openai-codex"]?.signed_in).toBe(false);
  });

  it("keeps a sign-in that belongs to the default profile and says so", async () => {
    mock.state.inheritedSignIns = ["openai-codex"];
    mock.state.signIns["openai-codex"] = {
      provider: "openai-codex",
      label: CHATGPT.label,
      signed_in: true,
      signed_in_at: null,
      flows: { browser: true, device: true },
      accounts: [
        {
          id: "acct-b",
          label: "home",
          source: "device_code",
          status: "ok",
          expires_at: null,
          expired: false,
          plan: null,
        },
      ],
    };
    renderPage(<EnvPage />, "/env");
    const row = await chatgptRow();
    const account = await row.findByTestId("sign-in-account-acct-b");
    expect(account.textContent).toBe("home");
    fireEvent.click(row.getByRole("button", { name: "Sign out" }));
    const dialog = within(await screen.findByRole("dialog"));
    fireEvent.click(dialog.getByRole("button", { name: "Sign out" }));
    expect((await row.findByRole("alert")).textContent).toContain("default profile");
    expect(row.getByTestId("sign-in-account-acct-b")).toBeTruthy();
  });
});

describe("the mock follows M1's contract", () => {
  it("refuses an API key for a sign-in-only provider", async () => {
    await expect(
      api.system.addToPool({ provider: "openai-codex", api_key: "x".repeat(24) }),
    ).rejects.toMatchObject({ status: 400, code: "sign_in_only" });
  });

  it("answers the accounts route for a known provider and 404s an unknown one", async () => {
    const answer = await api.providerLogin.accounts("openai-codex");
    expect(answer).toMatchObject({ provider: "openai-codex", signed_in: false, accounts: [] });
    await expect(api.providerLogin.accounts("nope")).rejects.toMatchObject({
      status: 404,
      code: "provider_not_found",
    });
    await expect(api.providerLogin.start("nope")).rejects.toMatchObject({
      status: 404,
      code: "unknown_provider",
    });
  });
});
