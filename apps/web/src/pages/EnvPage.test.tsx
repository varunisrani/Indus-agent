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
    const input = (await screen.findByLabelText("New value for MODEL_API_KEY")) as HTMLInputElement;
    expect(input.type).toBe("password");
    fireEvent.change(input, { target: { value: secret() } });
    fireEvent.click(
      within(screen.getByTestId("key-MODEL_API_KEY")).getByRole("button", { name: "Save" }),
    );

    const preview = await screen.findByTestId("preview-MODEL_API_KEY");
    expect(preview.textContent).toBe("valu…89");
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
    fireEvent.click(screen.getByRole("switch", { name: "Show advanced keys" }));
    expect(await screen.findByTestId("key-DEBUG_ENDPOINT")).toBeTruthy();
  });

  it("deletes a key only after confirming", async () => {
    renderPage(<EnvPage />, "/env");
    fireEvent.click(
      within(await screen.findByTestId("key-SEARCH_API_KEY")).getByRole("button", {
        name: "Delete",
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
});
