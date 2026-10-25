import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createMockApi, type MockApi } from "../testing/mockApi.ts";
import { renderPage } from "../testing/render.tsx";
import { APPLY_NOTE, ConfigPage, YAML_SAVE_NOTE } from "./ConfigPage.tsx";

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

function sections() {
  return within(screen.getByRole("navigation", { name: "Config sections" }));
}

async function openSection(name: RegExp) {
  await screen.findByRole("navigation", { name: "Config sections" });
  fireEvent.click(await sections().findByRole("button", { name }));
}

describe("config form", () => {
  it("renders the right control for each schema type, grouped by category", async () => {
    renderPage(<ConfigPage />, "/config");
    const provider = await screen.findByLabelText("model.provider");
    expect(provider.tagName).toBe("INPUT");
    expect((provider as HTMLInputElement).type).toBe("text");
    expect((provider as HTMLInputElement).value).toBe("fake");
    expect(
      sections()
        .getAllByRole("button")
        .map((b) => b.textContent),
    ).toEqual(["Model1", "Display2", "Agent1"]);
    expect(screen.getByRole("heading", { level: 2 }).textContent).toBe("Model");
    expect(screen.getByText("1 field")).toBeTruthy();

    await openSection(/Display/);
    const iface = screen.getByLabelText("display.interface");
    expect(iface.tagName).toBe("SELECT");
    expect(
      within(iface)
        .getAllByRole("option")
        .map((o) => o.textContent),
    ).toEqual(["cli", "tui"]);
    const bell = screen.getByRole("switch", { name: "display.bell" });
    expect(bell.getAttribute("aria-checked")).toBe("false");
    expect(screen.queryByLabelText("model.provider")).toBeNull();

    await openSection(/Agent/);
    const turns = screen.getByLabelText("agent.max_turns") as HTMLInputElement;
    expect(turns.type).toBe("number");
    expect(turns.value).toBe("20");
  });

  it("saves the edited values in one write, with the apply note in the toast", async () => {
    renderPage(<ConfigPage />, "/config");
    const save = screen.getByRole("button", { name: "Save" }) as HTMLButtonElement;
    fireEvent.change(await screen.findByLabelText("model.provider"), {
      target: { value: "other" },
    });
    await openSection(/Display/);
    fireEvent.click(screen.getByRole("switch", { name: "display.bell" }));
    fireEvent.change(screen.getByLabelText("display.interface"), { target: { value: "cli" } });
    await openSection(/Agent/);
    fireEvent.change(screen.getByLabelText("agent.max_turns"), { target: { value: "35" } });
    expect(save.disabled).toBe(false);
    fireEvent.click(save);
    await waitFor(() => expect(mock.callsTo("PUT", "/api/config")).toHaveLength(1));
    expect(mock.callsTo("PUT", "/api/config")[0]?.body).toEqual({
      config: {
        model: { provider: "other" },
        display: { interface: "cli", bell: true },
        agent: { max_turns: 35 },
      },
    });
    expect((await screen.findByText(/^Saved\./)).textContent).toContain(APPLY_NOTE);
    await waitFor(() => expect(save.disabled).toBe(true));
  });

  it("keeps Save off until something changes", async () => {
    renderPage(<ConfigPage />, "/config");
    await screen.findByLabelText("model.provider");
    expect((screen.getByRole("button", { name: "Save" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("resets the shown section to the defaults after a confirm, without saving", async () => {
    renderPage(<ConfigPage />, "/config");
    await openSection(/Display/);
    fireEvent.change(screen.getByLabelText("display.interface"), { target: { value: "tui" } });
    fireEvent.click(screen.getByRole("switch", { name: "display.bell" }));
    const before = mock.callsTo("PUT", "/api/config").length;
    fireEvent.click(screen.getByRole("button", { name: "Reset section to defaults" }));
    const dialog = within(await screen.findByRole("dialog"));
    expect(dialog.getByText(/2 fields of Display/)).toBeTruthy();
    const confirm = dialog.getByRole("button", { name: "Reset to defaults" }) as HTMLButtonElement;
    await waitFor(() => expect(confirm.disabled).toBe(false));
    fireEvent.click(confirm);
    await waitFor(() =>
      expect((screen.getByLabelText("display.interface") as HTMLSelectElement).value).toBe("cli"),
    );
    expect(screen.getByRole("switch", { name: "display.bell" }).getAttribute("aria-checked")).toBe(
      "false",
    );
    expect(mock.callsTo("GET", "/api/config/defaults")).toHaveLength(1);
    expect(mock.callsTo("PUT", "/api/config")).toHaveLength(before);
    // Other sections keep their values; Save is armed.
    await openSection(/Model/);
    expect((screen.getByLabelText("model.provider") as HTMLInputElement).value).toBe("fake");
    expect((screen.getByRole("button", { name: "Save" }) as HTMLButtonElement).disabled).toBe(
      false,
    );
  });

  it("searches keys, labels and descriptions across sections", async () => {
    renderPage(<ConfigPage />, "/config");
    await screen.findByLabelText("model.provider");
    const search = screen.getByRole("searchbox", { name: "Search settings" });
    fireEvent.change(search, { target: { value: "turn" } });
    expect(screen.getByRole("heading", { level: 2 }).textContent).toBe("Search results");
    expect(screen.getByLabelText("agent.max_turns")).toBeTruthy();
    expect(screen.queryByLabelText("model.provider")).toBeNull();
    fireEvent.change(search, { target: { value: "bell" } });
    expect(screen.getByRole("switch", { name: "display.bell" })).toBeTruthy();
    fireEvent.change(search, { target: { value: "nothing-like-this" } });
    expect(screen.getByText("No fields match 'nothing-like-this'")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Clear search" }));
    expect(screen.getByLabelText("model.provider")).toBeTruthy();
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

  it("shows the file's own settings as read-only YAML", async () => {
    mock.state.effective = [
      { key: "model.provider", value: "fake", source: "file" },
      { key: "model.name", value: "fake-large", source: "file" },
      { key: "display.bell", value: true, source: "env" },
      { key: "agent.max_turns", value: 90, source: "default" },
    ];
    renderPage(<ConfigPage />, "/config");
    await screen.findByLabelText("model.provider");
    fireEvent.click(screen.getByRole("button", { name: "YAML" }));
    const yaml = (await screen.findByRole("textbox", {
      name: "Raw YAML configuration",
    })) as HTMLTextAreaElement;
    await waitFor(() => expect(yaml.value).toBe("model:\n  provider: fake\n  name: fake-large\n"));
    expect(yaml.readOnly).toBe(true);
    const save = screen.getByRole("button", { name: "Save" }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    expect(save.title).toBe(YAML_SAVE_NOTE);
    expect(screen.queryByRole("button", { name: "Reset section to defaults" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Form" }));
    expect(await screen.findByLabelText("model.provider")).toBeTruthy();
  });

  it("says so when the file sets nothing", async () => {
    renderPage(<ConfigPage />, "/config");
    await screen.findByLabelText("model.provider");
    fireEvent.click(screen.getByRole("button", { name: "YAML" }));
    const yaml = (await screen.findByRole("textbox", {
      name: "Raw YAML configuration",
    })) as HTMLTextAreaElement;
    expect(yaml.value).toBe("# config.yaml has no settings yet\n");
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
    const file = within(screen.getByTestId("effective-model.provider")).getByText("config file");
    expect(file.className).toContain("whitespace-nowrap");
    expect(
      within(screen.getByTestId("effective-agent.max_turns")).getByText("default"),
    ).toBeTruthy();
  });
});
