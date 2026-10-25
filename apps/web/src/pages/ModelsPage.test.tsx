import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createMockApi, type MockApi } from "../testing/mockApi.ts";
import { currentLocation, renderPage } from "../testing/render.tsx";
import { ModelsPage, moaSummary } from "./ModelsPage.tsx";

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
  it("shows the current model and saves a new main model from the dialog", async () => {
    renderPage(<ModelsPage />, "/models");
    await waitFor(() =>
      expect(screen.getByTestId("current-model").textContent).toBe("fake · fake-model · 128K ctx"),
    );
    fireEvent.click(screen.getByRole("button", { name: "Change" }));
    const dialog = within(await screen.findByRole("dialog", { name: "Switch model" }));
    expect(dialog.getByText("current: fake-model · fake")).toBeTruthy();
    // The model in use is listed (and picked) for its provider; the field types another id.
    const models = within(await dialog.findByRole("list", { name: "Models" }));
    expect((await models.findByRole("button", { name: /fake-model/ })).textContent).toContain(
      "CURRENT",
    );
    const model = dialog.getByLabelText("Model id") as HTMLInputElement;
    expect((dialog.getByRole("button", { name: "Switch" }) as HTMLButtonElement).disabled).toBe(
      true,
    );
    fireEvent.change(model, { target: { value: "fake-large" } });
    fireEvent.click(dialog.getByRole("button", { name: "Switch" }));
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
    expect(screen.queryByRole("dialog", { name: "Switch model" })).toBeNull();
  });

  it("lists a custom endpoint's own models and keeps the typed id as a fallback (W57-H1-5)", async () => {
    mock.state.modelOptions.providers.push({
      name: "custom",
      label: "Custom endpoint",
      description: "any OpenAI-compatible endpoint",
      family: "custom",
      auth_type: "api_key_optional",
      configured: true,
      device_login: false,
      supports_model_listing: true,
    });
    mock.state.modelOptions.current = { provider: "custom", model: "fake-model" };
    mock.state.config = { model: { provider: "custom", base_url: "http://127.0.0.1:9/v1" } };
    mock.state.endpointModels["http://127.0.0.1:9/v1"] = ["fake-model", "fake-model-1"];
    renderPage(<ModelsPage />, "/models");
    await screen.findByTestId("provider-fake");
    fireEvent.click(screen.getByRole("button", { name: "Change" }));
    const dialog = within(await screen.findByRole("dialog", { name: "Switch model" }));
    const models = within(await dialog.findByRole("list", { name: "Models" }));
    await models.findByRole("button", { name: /fake-model-1/ });
    expect(mock.callsTo("POST", "/api/model/test-endpoint")[0]?.body).toEqual({
      base_url: "http://127.0.0.1:9/v1",
    });
    expect(models.getAllByRole("button").map((b) => b.textContent)).toEqual([
      "fake-modelCURRENT",
      "fake-model-1",
    ]);
    expect(dialog.getByTestId("model-list-hint").textContent).toBe(
      "Pick a model, or type another id.",
    );
    const providers = within(dialog.getByRole("list", { name: "Providers" }));
    expect(providers.getByRole("button", { name: /Custom endpoint/ }).textContent).toContain(
      "custom · 2 models",
    );
    expect(dialog.getByLabelText("Model id")).toBeTruthy();
    fireEvent.click(models.getByRole("button", { name: /fake-model-1/ }));
    fireEvent.click(dialog.getByRole("button", { name: "Switch" }));
    await waitFor(() =>
      expect(mock.callsTo("POST", "/api/model/set")[0]?.body).toEqual({
        scope: "main",
        provider: "custom",
        model: "fake-model-1",
      }),
    );
  });

  it("a saved endpoint that does not answer says so and leaves the typed id", async () => {
    mock.state.modelOptions.providers.push({
      name: "my-llm",
      label: "my-llm",
      description: "",
      family: "custom",
      auth_type: "api_key_optional",
      configured: true,
      device_login: false,
      supports_model_listing: true,
    });
    mock.state.config = {
      model: { provider: "fake" },
      providers: {
        "my-llm": {
          base_url: "http://127.0.0.1:7/v1",
          key_env: "MY_LLM_API_KEY",
          models: ["saved-one"],
        },
      },
    };
    renderPage(<ModelsPage />, "/models");
    await screen.findByTestId("provider-fake");
    fireEvent.click(screen.getByRole("button", { name: "Change" }));
    const dialog = within(await screen.findByRole("dialog", { name: "Switch model" }));
    const list = within(await dialog.findByRole("list", { name: "Providers" }));
    fireEvent.click(list.getByRole("button", { name: /my-llm/ }));
    await waitFor(() =>
      expect(dialog.getByTestId("model-list-hint").textContent).toBe(
        "The endpoint did not answer (ConnectError). Type the model id instead.",
      ),
    );
    expect(mock.callsTo("POST", "/api/model/test-endpoint")[0]?.body).toEqual({
      base_url: "http://127.0.0.1:7/v1",
      key_env: "MY_LLM_API_KEY",
    });
    // the entry's saved models are still offered
    const models = within(dialog.getByRole("list", { name: "Models" }));
    expect(models.getByRole("button", { name: /saved-one/ })).toBeTruthy();
  });

  it("a vendor provider asks no endpoint and explains the typed id", async () => {
    renderPage(<ModelsPage />, "/models");
    await screen.findByTestId("provider-fake");
    fireEvent.click(screen.getByRole("button", { name: "Change" }));
    const dialog = within(await screen.findByRole("dialog", { name: "Switch model" }));
    await dialog.findByRole("list", { name: "Models" });
    expect(dialog.getByTestId("model-list-hint").textContent).toContain("cannot list them");
    expect(mock.callsTo("POST", "/api/model/test-endpoint")).toEqual([]);
  });

  it("lists a vendor provider's models from the price table (W60-L1-1)", async () => {
    mock.state.modelOptions.providers.push({
      name: "vendor",
      label: "Vendor API",
      description: "",
      family: "vendor",
      auth_type: "api_key",
      configured: false,
      device_login: false,
      supports_model_listing: true,
      models: ["vendor-large", "vendor-small"],
      models_source: "price_table",
      models_checked: "2026-10-18",
    });
    renderPage(<ModelsPage />, "/models");
    await screen.findByTestId("provider-fake");
    fireEvent.click(screen.getByRole("button", { name: "Change" }));
    const dialog = within(await screen.findByRole("dialog", { name: "Switch model" }));
    const providers = within(await dialog.findByRole("list", { name: "Providers" }));
    // The row counts the listed models instead of the key state.
    const row = await providers.findByRole("button", { name: /Vendor API/ });
    expect(row.textContent).toContain("vendor · 2 models");
    expect(providers.getByRole("button", { name: /Sign-in provider/ }).textContent).toContain(
      "no key",
    );
    fireEvent.click(row);
    const models = within(dialog.getByRole("list", { name: "Models" }));
    expect(models.getAllByRole("button").map((b) => b.textContent)).toEqual([
      "vendor-large",
      "vendor-small",
    ]);
    expect(dialog.getByTestId("model-list-hint").textContent).toBe(
      "From our price table (checked 2026-10-18). Pick a model, or type another id.",
    );
    fireEvent.click(models.getByRole("button", { name: "vendor-small" }));
    fireEvent.click(dialog.getByRole("button", { name: "Switch" }));
    await waitFor(() =>
      expect(mock.callsTo("POST", "/api/model/set")[0]?.body).toEqual({
        scope: "main",
        provider: "vendor",
        model: "vendor-small",
      }),
    );
    expect(mock.callsTo("POST", "/api/model/test-endpoint")).toEqual([]);
  });

  it("picks another provider in the dialog and filters the list", async () => {
    renderPage(<ModelsPage />, "/models");
    await screen.findByTestId("provider-fake");
    fireEvent.click(screen.getByRole("button", { name: "Change" }));
    const dialog = within(await screen.findByRole("dialog", { name: "Switch model" }));
    const list = within(await dialog.findByRole("list", { name: "Providers" }));
    fireEvent.change(dialog.getByLabelText("Filter providers and models"), {
      target: { value: "sign" },
    });
    expect(list.queryByText("Fake provider")).toBeNull();
    fireEvent.click(list.getByRole("button", { name: /Sign-in provider/ }));
    fireEvent.change(dialog.getByLabelText("Model id"), { target: { value: "other-model" } });
    fireEvent.click(dialog.getByRole("button", { name: "Switch" }));
    await waitFor(() =>
      expect(mock.callsTo("POST", "/api/model/set")[0]?.body).toEqual({
        scope: "main",
        provider: "signin",
        model: "other-model",
      }),
    );
  });

  it("lists providers with their state and saves a side task", async () => {
    renderPage(<ModelsPage />, "/models");
    const fake = within(await screen.findByTestId("provider-fake"));
    expect(fake.getByText("configured")).toBeTruthy();
    expect(fake.getByText("main")).toBeTruthy();
    expect(fake.queryByRole("button", { name: "Sign in" })).toBeNull();
    expect(within(screen.getByTestId("provider-signin")).getByText("no key")).toBeTruthy();

    await waitFor(() =>
      expect(screen.getByTestId("aux-summary").textContent).toBe("1 task · all auto"),
    );
    fireEvent.click(screen.getByRole("button", { name: "Configure" }));
    const aux = within(await screen.findByTestId("aux-compression"));
    expect(aux.getByText("auto (main model)")).toBeTruthy();
    fireEvent.click(aux.getByRole("button", { name: "Change" }));
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
    await waitFor(() =>
      expect(screen.getByTestId("aux-summary").textContent).toBe("1 override · 0 auto"),
    );
  });

  it("resets only the overridden side tasks after a confirm", async () => {
    mock.state.auxiliary.tasks.push(
      { task: "title", provider: "fake", model: "fake-mini", base_url: "", description: "" },
      { task: "vision", provider: "", model: "fake-eye", base_url: "", description: "" },
    );
    renderPage(<ModelsPage />, "/models");
    await waitFor(() =>
      expect(screen.getByTestId("aux-summary").textContent).toBe("2 overrides · 1 auto"),
    );
    fireEvent.click(screen.getByRole("button", { name: "Configure" }));
    fireEvent.click(await screen.findByRole("button", { name: "Reset all to auto" }));
    const confirm = within(await screen.findByRole("dialog", { name: "Reset side tasks" }));
    expect(mock.callsTo("POST", "/api/model/set")).toHaveLength(0);
    fireEvent.click(confirm.getByRole("button", { name: "Reset" }));
    await waitFor(() => expect(mock.callsTo("POST", "/api/model/set")).toHaveLength(2));
    expect(mock.callsTo("POST", "/api/model/set").map((c) => c.body)).toEqual([
      { scope: "auxiliary", task: "title", provider: "auto", model: "" },
      { scope: "auxiliary", task: "vision", provider: "auto", model: "" },
    ]);
    await waitFor(() =>
      expect(screen.getByTestId("aux-summary").textContent).toBe("3 tasks · all auto"),
    );
  });

  it("shows usage for the period, sessions only while the token flag is off", async () => {
    mock.state.usage = [
      { day: "2026-10-24", input_tokens: 3400, output_tokens: 316, cost: 0.005, sessions: 3 },
    ];
    renderPage(<ModelsPage />, "/models");
    const figures = await screen.findByLabelText("Model usage");
    await waitFor(() => expect(figures.textContent).toContain("Total sessions"));
    expect(figures.textContent).not.toContain("Input");
    expect(screen.getByText("dashboard.show_token_analytics")).toBeTruthy();
    const header = within(screen.getByTestId("page-header-after-title"));
    fireEvent.click(header.getByRole("button", { name: "7d" }));
    await waitFor(() =>
      expect(mock.callsTo("GET", "/api/analytics/usage").at(-1)?.query.get("days")).toBe("7"),
    );
  });

  it("shows token figures when the flag is on", async () => {
    mock.state.config = { ...mock.state.config, dashboard: { show_token_analytics: true } };
    mock.state.usage = [
      { day: "2026-10-24", input_tokens: 3400, output_tokens: 316, cost: 0.005, sessions: 3 },
    ];
    renderPage(<ModelsPage />, "/models");
    const figures = await screen.findByLabelText("Model usage");
    await waitFor(() => expect(figures.textContent).toContain("3.7K"));
    expect(figures.textContent).toContain("$0.0050");
  });

  it("sums up the mixture-of-agents presets; Configure opens that Config section", async () => {
    mock.state.config = {
      ...mock.state.config,
      moa: {
        default_preset: "review",
        presets: {
          review: {
            reference_models: [
              { provider: "fake", model: "fake-a" },
              { provider: "fake", model: "fake-b" },
              { provider: "fake", model: "fake-c", enabled: false },
            ],
            aggregator: { provider: "fake", model: "fake-model" },
          },
        },
      },
    };
    renderPage(<ModelsPage />, "/models");
    await waitFor(() =>
      expect(screen.getByTestId("moa-summary").textContent).toBe("2 references · fake:fake-model"),
    );
    fireEvent.click(screen.getByRole("button", { name: "Configure mixture of agents" }));
    await screen.findByTestId("elsewhere");
    expect(currentLocation()).toBe("/config?section=moa");
  });

  it("shows a card per model for the period, tokens only with the flag", async () => {
    mock.state.usage = [
      { day: "2026-10-24", input_tokens: 3400, output_tokens: 316, cost: 0.005, sessions: 3 },
    ];
    mock.state.usageByModel = [
      { model: "fake-model", input_tokens: 3000, output_tokens: 300, api_calls: 7, cost: 0.004 },
      { model: "fake-mini", input_tokens: 400, output_tokens: 16, api_calls: 1, cost: 0.001 },
    ];
    const view = renderPage(<ModelsPage />, "/models");
    const cards = within(await screen.findByRole("list", { name: "Usage by model" }));
    const first = within(cards.getByTestId("model-usage-fake-model"));
    expect(first.getByText("#1")).toBeTruthy();
    expect(first.getByText("main")).toBeTruthy();
    expect(first.getByText("7")).toBeTruthy();
    expect(first.getByText("API calls")).toBeTruthy();
    const second = within(cards.getByTestId("model-usage-fake-mini"));
    expect(second.getByText("#2")).toBeTruthy();
    expect(second.queryByText("main")).toBeNull();
    expect(second.getByText("API call")).toBeTruthy();
    expect(cards.queryByText(/ in · /)).toBeNull();
    view.unmount();

    mock.state.config = { ...mock.state.config, dashboard: { show_token_analytics: true } };
    renderPage(<ModelsPage />, "/models");
    const shown = within(await screen.findByTestId("model-usage-fake-model"));
    await waitFor(() => expect(shown.getByText("3.0K in · 300 out · $0.0040")).toBeTruthy());
  });

  it("says so when no model was called in the period", async () => {
    renderPage(<ModelsPage />, "/models");
    expect(await screen.findByText("No model calls in this period.")).toBeTruthy();
    expect(screen.queryByRole("list", { name: "Usage by model" })).toBeNull();
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

describe("moaSummary", () => {
  it("reads the preset /moa would use", () => {
    expect(moaSummary(undefined)).toBe("no presets");
    expect(moaSummary({ moa: { presets: {} } })).toBe("no presets");
    const one = { reference_models: [{ provider: "p", model: "m" }] };
    expect(moaSummary({ moa: { presets: { solo: one } } })).toBe("1 reference · no aggregator");
    const two = { reference_models: [], aggregator: { model: "agg" } };
    expect(moaSummary({ moa: { presets: { solo: one, duo: two } } })).toBe(
      "2 presets · no default",
    );
    expect(moaSummary({ moa: { default_preset: "duo", presets: { solo: one, duo: two } } })).toBe(
      "duo · 0 references · agg",
    );
  });
});
