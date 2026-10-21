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
    current: "default",
    profiles: [
      {
        name: "default",
        is_default: true,
        is_sticky: true,
        is_current: true,
        model: "fake-model",
        provider: "fake",
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
      },
    ],
  };
  mock.state.personas = { work: "Be brief." };
  restore = mock.install();
  vi.spyOn(console, "warn").mockImplementation(() => {});
});

afterEach(() => {
  restore();
});

async function card(name: string) {
  return within(await screen.findByTestId(`profile-${name}`));
}

describe("profiles page", () => {
  it("shows badges, model and description, and locks the default profile", async () => {
    renderPage(<ProfilesPage />, "/profiles");
    const main = await card("default");
    expect(main.getByText("default", { selector: "span" })).toBeTruthy();
    expect(main.getByText("sticky")).toBeTruthy();
    expect(main.getByText("this dashboard")).toBeTruthy();
    expect(main.getByText(/fake-model \(fake\)/)).toBeTruthy();
    expect((main.getByRole("button", { name: "Delete" }) as HTMLButtonElement).disabled).toBe(true);
    expect((main.getByRole("button", { name: "Rename" }) as HTMLButtonElement).disabled).toBe(true);
    expect(main.queryByRole("button", { name: "Make sticky" })).toBeNull();

    const work = await card("work");
    expect(work.getByText("Office tasks")).toBeTruthy();
    expect(work.getByText(/inherited · has a persona/)).toBeTruthy();
  });

  it("makes a profile sticky", async () => {
    renderPage(<ProfilesPage />, "/profiles");
    const work = await card("work");
    fireEvent.click(work.getByRole("button", { name: "Make sticky" }));
    await waitFor(() =>
      expect(mock.callsTo("POST", "/api/profiles/active")[0]?.body).toEqual({ name: "work" }),
    );
    await waitFor(() => expect(work.getByText("sticky")).toBeTruthy());
  });

  it("renames inline and refuses an invalid name", async () => {
    renderPage(<ProfilesPage />, "/profiles");
    const work = await card("work");
    fireEvent.click(work.getByRole("button", { name: "Rename" }));
    const input = work.getByLabelText("New name");
    fireEvent.change(input, { target: { value: "default" } });
    expect(work.getByText(/reserved/)).toBeTruthy();
    expect((work.getByRole("button", { name: "Save name" }) as HTMLButtonElement).disabled).toBe(
      true,
    );
    fireEvent.change(input, { target: { value: "Office" } });
    fireEvent.click(work.getByRole("button", { name: "Save name" }));
    await waitFor(() =>
      expect(mock.callsTo("PATCH", "/api/profiles/work")[0]?.body).toEqual({ new_name: "office" }),
    );
    expect(await screen.findByTestId("profile-office")).toBeTruthy();
  });

  it("deletes only after the name is typed", async () => {
    renderPage(<ProfilesPage />, "/profiles");
    const work = await card("work");
    fireEvent.click(work.getByRole("button", { name: "Delete" }));
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

  it("edits model, description and persona", async () => {
    renderPage(<ProfilesPage />, "/profiles");
    const work = await card("work");
    fireEvent.click(work.getByRole("button", { name: "Edit" }));
    const dialog = within(await screen.findByRole("dialog"));
    await waitFor(() =>
      expect((dialog.getByLabelText("Persona (PERSONA.md)") as HTMLTextAreaElement).value).toBe(
        "Be brief.",
      ),
    );
    fireEvent.change(dialog.getByLabelText("Provider"), { target: { value: "fake" } });
    fireEvent.change(dialog.getByLabelText("Model"), { target: { value: "fake-large" } });
    fireEvent.click(dialog.getByRole("button", { name: "Save model" }));
    await waitFor(() =>
      expect(mock.callsTo("PUT", "/api/profiles/work/model")[0]?.body).toEqual({
        provider: "fake",
        model: "fake-large",
      }),
    );
    fireEvent.change(dialog.getByLabelText("Description"), { target: { value: "Desk work" } });
    fireEvent.click(dialog.getByRole("button", { name: "Save description" }));
    await waitFor(() =>
      expect(mock.callsTo("PUT", "/api/profiles/work/description")[0]?.body).toEqual({
        description: "Desk work",
      }),
    );
    fireEvent.change(dialog.getByLabelText("Persona (PERSONA.md)"), {
      target: { value: "Be brief and kind." },
    });
    fireEvent.click(dialog.getByRole("button", { name: "Save persona" }));
    await waitFor(() => expect(mock.state.personas.work).toBe("Be brief and kind."));
  });

  it("exports with the run tailed under that profile", async () => {
    renderPage(<ProfilesPage />, "/profiles");
    const work = await card("work");
    fireEvent.click(work.getByRole("button", { name: "Export" }));
    const log = await work.findByTestId("action-log-profile-export");
    await waitFor(() => expect(log.textContent).toContain("profile-export done"));
    const status = mock.callsTo("GET", "/api/actions/profile-export/status")[0];
    expect(status?.query.get("profile")).toBe("work");
    expect(work.getByText("/profiles/work/backups/profile-work.zip")).toBeTruthy();
  });

  it("imports an archive by path and links to the builder", async () => {
    renderPage(<ProfilesPage />, "/profiles");
    await card("work");
    fireEvent.change(screen.getByLabelText("Archive path"), { target: { value: "relative.zip" } });
    fireEvent.click(screen.getByRole("button", { name: "Import" }));
    expect((await screen.findByRole("alert")).textContent).toContain("full path");
    fireEvent.change(screen.getByLabelText("Archive path"), {
      target: { value: "/tmp/home.zip" },
    });
    fireEvent.change(screen.getByLabelText("Name (optional)"), { target: { value: "home" } });
    fireEvent.click(screen.getByRole("button", { name: "Import" }));
    await screen.findByTestId("profile-home");
    expect(mock.callsTo("POST", "/api/profiles/import")[0]?.body).toEqual({
      archive: "/tmp/home.zip",
      name: "home",
    });

    fireEvent.click(screen.getByRole("link", { name: "New profile" }));
    await waitFor(() => expect(currentLocation()).toBe("/profiles/new"));
  });
});
