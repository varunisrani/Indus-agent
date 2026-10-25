import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createMockApi, type MockApi } from "../testing/mockApi.ts";
import { currentLocation, renderPage } from "../testing/render.tsx";
import { ProfilesPage } from "./ProfilesPage.tsx";

let mock: MockApi;
let restore: () => void;

beforeEach(() => {
  mock = createMockApi();
  mock.state.profiles = {
    active: "default",
    current: "home",
    profiles: [
      {
        name: "default",
        is_default: true,
        is_sticky: true,
        is_current: false,
        model: "fake-model",
        provider: "fake",
        description: null,
        has_persona: false,
        skill_count: 1,
      },
      {
        name: "home",
        is_default: false,
        is_sticky: false,
        is_current: true,
        model: null,
        provider: null,
        description: null,
        has_persona: false,
      },
      {
        name: "work",
        is_default: false,
        is_sticky: false,
        is_current: false,
        model: null,
        provider: null,
        description: "Office tasks",
        has_persona: true,
        skill_count: null,
      },
    ],
  };
  mock.state.personas = { work: "Be brief." };
  mock.state.skills = [
    { name: "web-search", description: "", category: "web", enabled: true },
    { name: "notes", description: "", category: "notes", enabled: false },
  ];
  restore = mock.install();
  vi.spyOn(console, "warn").mockImplementation(() => {});
});

afterEach(() => {
  restore();
});

async function card(name: string) {
  return within(await screen.findByTestId(`profile-${name}`));
}

/** Open a card's actions menu and return it. */
async function openMenu(name: string) {
  const c = await card(name);
  fireEvent.click(c.getByRole("button", { name: "Actions" }));
  return within(c.getByRole("menu"));
}

describe("profiles page", () => {
  it("shows the active banner, badges, daemon line, model and skills count", async () => {
    renderPage(<ProfilesPage />, "/profiles");
    const main = await card("default");
    expect(screen.getByRole("status").textContent).toContain("Active profile: default");
    expect(screen.getByRole("status").textContent).toContain("(serving home)");
    expect(screen.getByRole("heading", { name: "Profiles (3)" })).toBeTruthy();
    expect(main.getByText("active")).toBeTruthy();
    expect(main.getByText("default", { selector: "span" })).toBeTruthy();
    expect(main.getByText(/fake-model \(fake\)/)).toBeTruthy();
    expect(main.getByText("No description")).toBeTruthy();
    expect(await main.findByText(/Daemon (running|stopped)/)).toBeTruthy();
    expect(await main.findByText("Skills: 1")).toBeTruthy();
    // The count comes with the profile row (W56-G2-2): no per-card `/api/skills` round trip.
    expect(mock.callsTo("GET", "/api/skills")).toHaveLength(0);
    expect(mock.callsTo("GET", "/api/status").some((c) => c.query.get("profile") === "work")).toBe(
      true,
    );

    expect((await card("home")).getByText("this dashboard")).toBeTruthy();
    const work = await card("work");
    expect(work.getByText("Office tasks")).toBeTruthy();
    expect(work.getByText("Model: inherited")).toBeTruthy();
    // An uncounted profile (null) shows no skills line rather than a wrong zero.
    expect(work.queryByText(/^Skills:/)).toBeNull();
  });

  it("never offers rename or delete for the default, and locks the served profile", async () => {
    renderPage(<ProfilesPage />, "/profiles");
    const main = await openMenu("default");
    expect(main.queryByRole("menuitem", { name: /set as active/i })).toBeNull();
    expect(main.queryByRole("menuitem", { name: /rename/i })).toBeNull();
    expect(main.queryByRole("menuitem", { name: /delete/i })).toBeNull();
    expect(main.getAllByRole("menuitem").map((item) => item.textContent)).toEqual([
      "Change model",
      "Edit description",
      "PEdit persona",
      "Manage skills & tools",
      "Copy CLI command",
      "Export",
    ]);
    fireEvent.keyDown(window, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());

    const home = await openMenu("home");
    expect((home.getByRole("menuitem", { name: /rename/i }) as HTMLButtonElement).disabled).toBe(
      true,
    );
    expect((home.getByRole("menuitem", { name: /delete/i }) as HTMLButtonElement).disabled).toBe(
      true,
    );
  });

  it("sets a profile as active from its menu", async () => {
    renderPage(<ProfilesPage />, "/profiles");
    const menu = await openMenu("work");
    fireEvent.click(menu.getByRole("menuitem", { name: /set as active/i }));
    expect(screen.queryByRole("menu")).toBeNull();
    await waitFor(() =>
      expect(mock.callsTo("POST", "/api/profiles/active")[0]?.body).toEqual({ name: "work" }),
    );
    const work = await card("work");
    await waitFor(() => expect(work.getByText("active")).toBeTruthy());
  });

  it("renames inline and refuses an invalid name", async () => {
    renderPage(<ProfilesPage />, "/profiles");
    const menu = await openMenu("work");
    fireEvent.click(menu.getByRole("menuitem", { name: /rename/i }));
    const work = await card("work");
    const input = work.getByLabelText("New name");
    fireEvent.change(input, { target: { value: "default" } });
    expect(work.getByText(/reserved/)).toBeTruthy();
    expect((work.getByRole("button", { name: "Save" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.keyDown(input, { key: "Escape" });
    expect(work.queryByLabelText("New name")).toBeNull();

    fireEvent.click((await openMenu("work")).getByRole("menuitem", { name: /rename/i }));
    fireEvent.change(work.getByLabelText("New name"), { target: { value: "Office" } });
    fireEvent.click(work.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(mock.callsTo("PATCH", "/api/profiles/work")[0]?.body).toEqual({ new_name: "office" }),
    );
    expect(await screen.findByTestId("profile-office")).toBeTruthy();
  });

  it("deletes only after the name is typed", async () => {
    renderPage(<ProfilesPage />, "/profiles");
    fireEvent.click((await openMenu("work")).getByRole("menuitem", { name: /delete/i }));
    const dialog = within(await screen.findByRole("dialog"));
    const confirm = dialog.getByRole("button", { name: "Delete" }) as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);
    fireEvent.change(dialog.getByLabelText("Type work to confirm"), { target: { value: "wor" } });
    expect(confirm.disabled).toBe(true);
    fireEvent.change(dialog.getByLabelText("Type work to confirm"), { target: { value: "work" } });
    expect(confirm.disabled).toBe(false);
    fireEvent.click(confirm);
    await waitFor(() => expect(screen.queryByTestId("profile-work")).toBeNull());
    expect(mock.callsTo("DELETE", "/api/profiles/work")).toHaveLength(1);
  });

  it("edits model, description and persona, each in its own dialog", async () => {
    renderPage(<ProfilesPage />, "/profiles");
    fireEvent.click((await openMenu("work")).getByRole("menuitem", { name: /change model/i }));
    let dialog = within(await screen.findByRole("dialog"));
    expect(dialog.getByRole("heading", { name: "Change model · work" })).toBeTruthy();
    const save = dialog.getByRole("button", { name: "Save" }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    await dialog.findByRole("option", { name: "Fake provider" });
    fireEvent.change(dialog.getByLabelText("Provider"), { target: { value: "fake" } });
    fireEvent.change(dialog.getByLabelText("Model"), { target: { value: "fake-large" } });
    fireEvent.click(save);
    await waitFor(() =>
      expect(mock.callsTo("PUT", "/api/profiles/work/model")[0]?.body).toEqual({
        provider: "fake",
        model: "fake-large",
      }),
    );
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    fireEvent.click((await openMenu("work")).getByRole("menuitem", { name: /edit description/i }));
    dialog = within(await screen.findByRole("dialog"));
    fireEvent.change(dialog.getByLabelText("Description"), { target: { value: "Desk work" } });
    fireEvent.click(dialog.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(mock.callsTo("PUT", "/api/profiles/work/description")[0]?.body).toEqual({
        description: "Desk work",
      }),
    );
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    fireEvent.click((await openMenu("work")).getByRole("menuitem", { name: /edit persona/i }));
    dialog = within(await screen.findByRole("dialog"));
    await waitFor(() =>
      expect((dialog.getByLabelText("Persona (PERSONA.md)") as HTMLTextAreaElement).value).toBe(
        "Be brief.",
      ),
    );
    fireEvent.change(dialog.getByLabelText("Persona (PERSONA.md)"), {
      target: { value: "Be brief and kind." },
    });
    fireEvent.click(dialog.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(mock.state.personas.work).toBe("Be brief and kind."));
  });

  it("exports with the run tailed under that profile", async () => {
    renderPage(<ProfilesPage />, "/profiles");
    fireEvent.click((await openMenu("work")).getByRole("menuitem", { name: "Export" }));
    const work = await card("work");
    const log = await work.findByTestId("action-log-profile-export");
    await waitFor(() => expect(log.textContent).toContain("profile-export done"));
    const status = mock.callsTo("GET", "/api/actions/profile-export/status")[0];
    expect(status?.query.get("profile")).toBe("work");
    expect(work.getByText("/profiles/work/backups/profile-work.zip")).toBeTruthy();
  });

  it("copies the CLI command and opens the profile's skills", async () => {
    const writeText = vi.fn(() => Promise.resolve());
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    renderPage(<ProfilesPage />, "/profiles");
    fireEvent.click((await openMenu("work")).getByRole("menuitem", { name: /copy cli command/i }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("indus -p work"));
    expect(await screen.findByText("Command copied: indus -p work")).toBeTruthy();

    fireEvent.click((await openMenu("work")).getByRole("menuitem", { name: /manage skills/i }));
    await waitFor(() => expect(currentLocation()).toBe("/skills?profile=work"));
  });

  it("imports an archive by path from the Import dialog", async () => {
    renderPage(<ProfilesPage />, "/profiles");
    await card("work");
    fireEvent.click(screen.getByRole("button", { name: "Import" }));
    const dialog = within(await screen.findByRole("dialog"));
    fireEvent.change(dialog.getByLabelText("Archive path"), { target: { value: "relative.zip" } });
    fireEvent.click(dialog.getByRole("button", { name: "Import" }));
    expect((await dialog.findByRole("alert")).textContent).toContain("full path");
    fireEvent.change(dialog.getByLabelText("Archive path"), {
      target: { value: "/tmp/home2.zip" },
    });
    fireEvent.change(dialog.getByLabelText("Name (optional)"), { target: { value: "home2" } });
    fireEvent.click(dialog.getByRole("button", { name: "Import" }));
    await screen.findByTestId("profile-home2");
    expect(mock.callsTo("POST", "/api/profiles/import")[0]?.body).toEqual({
      archive: "/tmp/home2.zip",
      name: "home2",
    });
  });

  it("creates a profile from the Create dialog and opens the builder from Build", async () => {
    renderPage(<ProfilesPage />, "/profiles");
    await card("work");
    fireEvent.click(screen.getByRole("button", { name: "Create" }));
    const dialog = within(await screen.findByRole("dialog"));
    const create = dialog.getByRole("button", { name: "Create" }) as HTMLButtonElement;
    expect(create.disabled).toBe(true);
    fireEvent.change(dialog.getByLabelText("Name"), { target: { value: "work" } });
    expect(dialog.getByText(/already exists/)).toBeTruthy();
    fireEvent.change(dialog.getByLabelText("Name"), { target: { value: "lab" } });
    expect((dialog.getByLabelText("Clone from") as HTMLSelectElement).value).toBe("default");
    fireEvent.change(dialog.getByLabelText("Description (optional)"), {
      target: { value: "Experiments" },
    });
    fireEvent.click(create);
    await screen.findByTestId("profile-lab");
    expect(mock.callsTo("POST", "/api/profiles")[0]?.body).toEqual({
      name: "lab",
      clone_from: "default",
      description: "Experiments",
    });

    fireEvent.click(screen.getByRole("button", { name: "Build" }));
    await waitFor(() => expect(currentLocation()).toBe("/profiles/new"));
  });
});
