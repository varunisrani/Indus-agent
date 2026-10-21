import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createMockApi, type MockApi } from "../testing/mockApi.ts";
import { renderPage } from "../testing/render.tsx";
import { ModelsPage } from "./ModelsPage.tsx";

let mock: MockApi;
let restore: () => void;

beforeEach(() => {
  mock = createMockApi();
  mock.state.modelOptions.providers.push({
    name: "signin",
    label: "Sign-in provider",
    description: "Signs in with a code",
    family: "openai",
    auth_type: "oauth",
    configured: false,
    device_login: true,
    supports_model_listing: false,
  });
  mock.state.auxiliary.tasks = [
    {
      task: "compression",
      provider: "auto",
      model: "",
      base_url: "",
      description: "Summaries of long sessions",
    },
  ];
  mock.state.deviceGrant = {
    user_code: "WXYZ-4321",
    verification_url: "https://signin.example.org/device",
    expires_in: 900,
    interval: 5,
  };
  restore = mock.install();
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

async function startSignIn() {
  const row = within(await screen.findByTestId("provider-signin"));
  fireEvent.click(row.getByRole("button", { name: "Sign in" }));
  fireEvent.click(row.getByRole("button", { name: "Get a sign-in code" }));
  expect((await row.findByTestId("device-code")).textContent).toBe("WXYZ-4321");
  return row;
}

describe("models page", () => {
  it("shows the current model and saves a new main model", async () => {
    renderPage(<ModelsPage />, "/models");
    expect((await screen.findByTestId("current-model")).textContent).toBe(
      "fake-model · fake · 128,000 token context",
    );
    const model = screen.getByLabelText("Model id") as HTMLInputElement;
    await waitFor(() => expect(model.value).toBe("fake-model"));
    fireEvent.change(model, { target: { value: "fake-large" } });
    fireEvent.click(screen.getByRole("button", { name: "Save model" }));
    await waitFor(() =>
      expect(mock.callsTo("POST", "/api/model/set")[0]?.body).toEqual({
        scope: "main",
        provider: "fake",
        model: "fake-large",
      }),
    );
    await waitFor(() =>
      expect(screen.getByTestId("current-model").textContent).toContain("fake-large"),
    );
  });

  it("lists providers with their state and saves a side task", async () => {
    renderPage(<ModelsPage />, "/models");
    const fake = within(await screen.findByTestId("provider-fake"));
    expect(fake.getByText("configured")).toBeTruthy();
    expect(fake.queryByRole("button", { name: "Sign in" })).toBeNull();
    expect(within(screen.getByTestId("provider-signin")).getByText("no key")).toBeTruthy();

    const aux = within(await screen.findByTestId("aux-compression"));
    fireEvent.change(aux.getByLabelText("Model"), { target: { value: "fake-mini" } });
    fireEvent.click(aux.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(mock.callsTo("POST", "/api/model/set")[0]?.body).toEqual({
        scope: "auxiliary",
        task: "compression",
        provider: "auto",
        model: "fake-mini",
      }),
    );
  });

  it("polls at the server's interval and stops when approved", async () => {
    mock.state.loginPolls = ["pending", "approved"];
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    renderPage(<ModelsPage />, "/models");
    const row = await startSignIn();
    const link = row.getByRole("link", { name: "https://signin.example.org/device" });
    expect(link.getAttribute("target")).toBe("_blank");
    expect(mock.callsTo("POST", "/api/providers/login/start")[0]?.body).toEqual({
      provider: "signin",
    });

    tick(4_999);
    expect(mock.callsTo("GET", "/api/providers/login/login-1")).toHaveLength(0);
    tick(1);
    await waitFor(() =>
      expect(mock.callsTo("GET", "/api/providers/login/login-1")).toHaveLength(1),
    );
    expect(row.getByRole("status").textContent).toContain("Waiting");
    tick(5_000);
    await waitFor(() => expect(row.getByRole("status").textContent).toContain("Signed in"));
    tick(20_000);
    expect(mock.callsTo("GET", "/api/providers/login/login-1")).toHaveLength(2);
  });

  it("stops polling and cancels the session on Cancel", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    renderPage(<ModelsPage />, "/models");
    const row = await startSignIn();
    tick(5_000);
    await waitFor(() =>
      expect(mock.callsTo("GET", "/api/providers/login/login-1")).toHaveLength(1),
    );
    fireEvent.click(row.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(row.getByRole("status").textContent).toContain("cancelled"));
    expect(mock.callsTo("DELETE", "/api/providers/login/login-1")).toHaveLength(1);
    tick(30_000);
    expect(mock.callsTo("GET", "/api/providers/login/login-1")).toHaveLength(1);
  });

  it("never polls faster than once a second", async () => {
    mock.state.deviceGrant = {
      ...(mock.state.deviceGrant as NonNullable<typeof mock.state.deviceGrant>),
      interval: 0,
    };
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    renderPage(<ModelsPage />, "/models");
    await startSignIn();
    tick(999);
    expect(mock.callsTo("GET", "/api/providers/login/login-1")).toHaveLength(0);
    tick(1);
    await waitFor(() =>
      expect(mock.callsTo("GET", "/api/providers/login/login-1")).toHaveLength(1),
    );
  });

  it("explains a provider without a device sign-in", async () => {
    mock.state.deviceGrant = null;
    renderPage(<ModelsPage />, "/models");
    const row = within(await screen.findByTestId("provider-signin"));
    fireEvent.click(row.getByRole("button", { name: "Sign in" }));
    fireEvent.click(row.getByRole("button", { name: "Get a sign-in code" }));
    expect((await row.findByRole("alert")).textContent).toContain("no device sign-in");
  });
});
