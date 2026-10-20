import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createMockApi, type MockApi } from "../testing/mockApi.ts";
import { renderPage } from "../testing/render.tsx";
import { APPLY_NOTE, ConfigPage } from "./ConfigPage.tsx";

let mock: MockApi;
let restore: () => void;

beforeEach(() => {
  mock = createMockApi();
  mock.state.schema.fields["agent.max_turns"] = {
    type: "integer",
    description: "Turn cap",
    category: "agent",
  };
  mock.state.schema.category_order = ["model", "display", "agent"];
  mock.state.config = { ...mock.state.config, agent: { max_turns: 20 } };
  restore = mock.install();
  vi.spyOn(console, "warn").mockImplementation(() => {});
});

afterEach(() => {
  restore();
});

describe("config form", () => {
  it("renders the right control for each schema type, grouped by category", async () => {
    renderPage(<ConfigPage />, "/config");
    const provider = await screen.findByLabelText("model.provider");
    expect(provider.tagName).toBe("INPUT");
    expect((provider as HTMLInputElement).type).toBe("text");
    expect((provider as HTMLInputElement).value).toBe("fake");

    const iface = screen.getByLabelText("display.interface");
    expect(iface.tagName).toBe("SELECT");
    expect(
      within(iface)
        .getAllByRole("option")
        .map((o) => o.textContent),
    ).toEqual(["cli", "tui"]);

    const bell = screen.getByRole("switch", { name: "display.bell" });
    expect(bell.getAttribute("aria-checked")).toBe("false");

    const turns = screen.getByLabelText("agent.max_turns") as HTMLInputElement;
    expect(turns.type).toBe("number");
    expect(turns.value).toBe("20");

    const headings = screen.getAllByRole("heading", { level: 2 }).map((h) => h.textContent);
    expect(headings).toEqual(["model", "display", "agent"]);
    expect(screen.getByText(APPLY_NOTE)).toBeTruthy();
  });

  it("saves the edited values in one write", async () => {
    renderPage(<ConfigPage />, "/config");
    fireEvent.change(await screen.findByLabelText("model.provider"), {
      target: { value: "other" },
    });
    fireEvent.click(screen.getByRole("switch", { name: "display.bell" }));
    fireEvent.change(screen.getByLabelText("display.interface"), { target: { value: "cli" } });
    fireEvent.change(screen.getByLabelText("agent.max_turns"), { target: { value: "35" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(mock.callsTo("PUT", "/api/config")).toHaveLength(1));
    expect(mock.callsTo("PUT", "/api/config")[0]?.body).toEqual({
      config: {
        model: { provider: "other" },
        display: { interface: "cli", bell: true },
        agent: { max_turns: 35 },
      },
    });
    expect(await screen.findByText(/^Saved\./)).toBeTruthy();
  });

  it("resets the form without calling the API", async () => {
    renderPage(<ConfigPage />, "/config");
    const provider = (await screen.findByLabelText("model.provider")) as HTMLInputElement;
    fireEvent.change(provider, { target: { value: "edited" } });
    expect(provider.value).toBe("edited");
    const before = mock.calls.length;
    fireEvent.click(screen.getByRole("button", { name: "Reset" }));
    expect((screen.getByLabelText("model.provider") as HTMLInputElement).value).toBe("fake");
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(mock.calls.length).toBe(before);
    expect((screen.getByRole("button", { name: "Save" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("imports a JSON file into the form without saving it", async () => {
    renderPage(<ConfigPage />, "/config");
    await screen.findByLabelText("model.provider");
    const file = new File([JSON.stringify({ model: { provider: "imported" } })], "c.json", {
      type: "application/json",
    });
    fireEvent.change(screen.getByLabelText("Import JSON"), { target: { files: [file] } });
    await waitFor(() =>
      expect((screen.getByLabelText("model.provider") as HTMLInputElement).value).toBe("imported"),
    );
    expect(mock.callsTo("PUT", "/api/config")).toHaveLength(0);
  });

  it("shows the effective value and where it comes from", async () => {
    mock.state.effective = [
      { key: "model.provider", value: "fake", source: "file" },
      { key: "display.bell", value: true, source: "env" },
      { key: "agent.max_turns", value: 90, source: "default" },
    ];
    renderPage(<ConfigPage />, "/config");
    fireEvent.click(await screen.findByRole("button", { name: "Effective" }));
    const bell = await screen.findByTestId("effective-display.bell");
    expect(within(bell).getByText("environment")).toBeTruthy();
    expect(
      within(screen.getByTestId("effective-model.provider")).getByText("config file"),
    ).toBeTruthy();
    expect(
      within(screen.getByTestId("effective-agent.max_turns")).getByText("default"),
    ).toBeTruthy();
  });
});
