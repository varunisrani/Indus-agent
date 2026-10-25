import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { EnvKeyInfo } from "../lib/restTypes.ts";
import { createMockApi, type MockApi } from "../testing/mockApi.ts";
import { renderPage } from "../testing/render.tsx";
import { EnvPage } from "./EnvPage.tsx";

let mock: MockApi;
let restore: () => void;

// A fake secret assembled at runtime (no credential-shaped literal in the source).
const secret = () => ["value", "for", "tests", "0123456789"].join("-");

function key(extra: Partial<EnvKeyInfo> = {}): EnvKeyInfo {
  return {
    is_set: false,
    redacted_value: null,
    description: "",
    url: null,
    category: "providers",
    is_password: true,
    advanced: false,
    ...extra,
  };
}

beforeEach(() => {
  mock = createMockApi();
  mock.state.env = {
    MODEL_API_KEY: key({
      description: "Key for the model provider",
      url: "https://example.org/keys",
    }),
    SEARCH_API_KEY: key({ is_set: true, redacted_value: "abcd…yz", category: "tools" }),
    DEBUG_ENDPOINT: key({ advanced: true, category: "advanced" }),
  };
  restore = mock.install();
  vi.spyOn(console, "warn").mockImplementation(() => {});
});

afterEach(() => {
  restore();
});

describe("keys", () => {
  it("clears the input after save and shows only the redacted preview", async () => {
    renderPage(<EnvPage />, "/env");
    // An unset key shows no input until Set is pressed.
    const row = within(await screen.findByTestId("key-MODEL_API_KEY"));
    expect(row.queryByLabelText("New value for MODEL_API_KEY")).toBeNull();
    fireEvent.click(row.getByRole("button", { name: "Set" }));
    const input = (await screen.findByLabelText("New value for MODEL_API_KEY")) as HTMLInputElement;
    expect(input.type).toBe("password");
    fireEvent.change(input, { target: { value: secret() } });
    fireEvent.click(
      within(screen.getByTestId("key-MODEL_API_KEY")).getByRole("button", { name: "Save" }),
    );

    const preview = await screen.findByTestId("preview-MODEL_API_KEY");
    expect(preview.textContent).toBe("valu…89");
    // The editor closes after a save; Replace opens an empty one.
    expect(screen.queryByLabelText("New value for MODEL_API_KEY")).toBeNull();
    fireEvent.click(
      within(screen.getByTestId("key-MODEL_API_KEY")).getByRole("button", { name: "Replace" }),
    );
    expect((screen.getByLabelText("New value for MODEL_API_KEY") as HTMLInputElement).value).toBe(
      "",
    );
    expect(document.body.innerHTML).not.toContain(secret());
    expect(mock.callsTo("PUT", "/api/env")[0]?.body).toEqual({
      key: "MODEL_API_KEY",
      value: secret(),
    });
    expect(mock.state.envValues.MODEL_API_KEY).toBe(secret());
  });

  it("shows set state, description and signup link", async () => {
    renderPage(<EnvPage />, "/env");
    const row = within(await screen.findByTestId("key-MODEL_API_KEY"));
    expect(row.getByText("not set")).toBeTruthy();
    expect(row.getByText("Key for the model provider")).toBeTruthy();
    expect(row.getByRole("link", { name: /Get a key/ }).getAttribute("href")).toBe(
      "https://example.org/keys",
    );
    expect(within(screen.getByTestId("key-SEARCH_API_KEY")).getByText("set")).toBeTruthy();
  });

  it("hides advanced keys until the toggle is on", async () => {
    renderPage(<EnvPage />, "/env");
    await screen.findByTestId("key-MODEL_API_KEY");
    expect(screen.queryByTestId("key-DEBUG_ENDPOINT")).toBeNull();
    const toggle = screen.getByRole("button", { name: "Show advanced" });
    expect(toggle.getAttribute("aria-pressed")).toBe("false");
    fireEvent.click(toggle);
    expect(await screen.findByTestId("key-DEBUG_ENDPOINT")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Hide advanced" }).getAttribute("aria-pressed")).toBe(
      "true",
    );
  });

  it("deletes a key only after confirming", async () => {
    renderPage(<EnvPage />, "/env");
    fireEvent.click(
      within(await screen.findByTestId("key-SEARCH_API_KEY")).getByRole("button", {
        name: "Clear",
      }),
    );
    const dialog = await screen.findByRole("dialog");
    expect(mock.callsTo("DELETE", "/api/env")).toHaveLength(0);
    fireEvent.click(within(dialog).getByRole("button", { name: "Remove" }));
    await waitFor(() => expect(mock.callsTo("DELETE", "/api/env")).toHaveLength(1));
    expect(mock.callsTo("DELETE", "/api/env")[0]?.body).toEqual({ key: "SEARCH_API_KEY" });
    await waitFor(() =>
      expect(within(screen.getByTestId("key-SEARCH_API_KEY")).getByText("not set")).toBeTruthy(),
    );
  });

  it("groups provider keys per provider and lists sign-in providers", async () => {
    mock.state.env = {
      ALPHA_API_KEY: key({ category: "provider", description: "API key for Alpha AI" }),
      ALPHA_BASE_URL: key({
        category: "provider",
        description: "Endpoint URL override for Alpha AI",
        is_set: true,
        redacted_value: "http…ai",
      }),
      OWN_TOKEN: key({ category: "custom", is_set: true, redacted_value: "own…en" }),
    };
    renderPage(<EnvPage />, "/env");
    const group = await screen.findByRole("button", { name: /Alpha AI/ });
    expect(group.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByTestId("key-ALPHA_API_KEY")).toBeNull();
    expect(within(group).getByText("1 set")).toBeTruthy();
    fireEvent.click(group);
    expect(await screen.findByTestId("key-ALPHA_API_KEY")).toBeTruthy();
    expect(
      within(screen.getByTestId("key-OWN_TOKEN")).getByTestId("preview-OWN_TOKEN"),
    ).toBeTruthy();
    const nav = within(screen.getByRole("navigation", { name: "Jump to section" }));
    expect(nav.getAllByRole("button").map((b) => b.textContent)).toEqual([
      "OAuth",
      "Providers",
      "Custom keys",
    ]);
    expect(await screen.findByRole("region", { name: "Provider sign-ins" })).toBeTruthy();
  });

  it("lists providers with a documented sign-in and their CLI command", async () => {
    mock.state.modelOptions.providers.push({
      name: "openrouter",
      label: "OpenRouter",
      description: "",
      family: "openai",
      auth_type: "api_key",
      configured: false,
      device_login: false,
      sign_in: true,
      supports_model_listing: true,
    });
    renderPage(<EnvPage />, "/env");
    const row = within(await screen.findByTestId("oauth-openrouter"));
    expect(row.getByText("Browser sign-in")).toBeTruthy();
    expect(row.getByText("indus auth add openrouter --type oauth")).toBeTruthy();
    expect(row.queryByRole("button", { name: "Login" })).toBeNull();
    expect(screen.queryByTestId("oauth-fake")).toBeNull();
  });

  it("adds a custom key by name and refuses a name that is not an env name", async () => {
    renderPage(<EnvPage />, "/env");
    const card = within(await screen.findByRole("region", { name: "Custom keys" }));
    fireEvent.change(card.getByLabelText("Add a key"), { target: { value: "9bad" } });
    fireEvent.click(card.getByRole("button", { name: "Add" }));
    expect((await card.findByRole("alert")).textContent).toContain("not starting with a digit");
    fireEvent.change(card.getByLabelText("Add a key"), { target: { value: "my_token" } });
    fireEvent.click(card.getByRole("button", { name: "Add" }));
    const input = await card.findByLabelText("New value for MY_TOKEN");
    fireEvent.change(input, { target: { value: secret() } });
    fireEvent.click(within(card.getByTestId("key-MY_TOKEN")).getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(mock.callsTo("PUT", "/api/env")[0]?.body).toEqual({
        key: "MY_TOKEN",
        value: secret(),
      }),
    );
    expect(document.body.innerHTML).not.toContain(secret());
  });
});
