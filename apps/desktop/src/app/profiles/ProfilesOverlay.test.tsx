// @vitest-environment jsdom
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { fakeRest } from "../../settings/fakeRest.ts";
import { type ProfileChanges, ProfilesOverlay } from "./ProfilesOverlay.tsx";

function backend() {
  const api = fakeRest();
  api.route("GET", "/api/profiles", {
    profiles: [
      { name: "default", is_default: true },
      { name: "coder", is_default: false, has_persona: false },
    ],
  });
  api.route("GET", "/api/profiles/coder/persona", { content: "", exists: false });
  api.route("PUT", "/api/profiles/coder/persona", { ok: true });
  api.route("POST", "/api/profiles/coder/export", (request) => ({
    status: 202,
    body: {
      ok: true,
      action: "profile-export",
      profile: "coder",
      archive: "/h/coder/backups/p.zip",
      body: request.body,
    },
  }));
  api.route("DELETE", "/api/profiles/coder", { ok: true, name: "coder", message: "" });
  return api;
}

async function coderCard(): Promise<HTMLElement> {
  await waitFor(() => expect(document.querySelector("[data-profile='coder']")).not.toBeNull());
  return document.querySelector("[data-profile='coder']") as HTMLElement;
}

function changes(): ProfileChanges & { runs: string[] } {
  const runs: string[] = [];
  return {
    runs,
    renameProfile: async (from, to, run) => {
      runs.push(`rename ${from}->${to}`);
      await run();
      return { ok: true };
    },
    deleteProfile: async (name, run) => {
      runs.push(`delete ${name}`);
      await run();
      return { ok: true };
    },
  };
}

describe("ProfilesOverlay", () => {
  it("export sends no key material and says the archive has none", async () => {
    const api = backend();
    render(<ProfilesOverlay api={api} onClose={() => {}} changes={changes()} />);
    const card = await coderCard();
    fireEvent.click(
      Array.from(card.querySelectorAll("button")).find(
        (b) => b.textContent === "Export",
      ) as HTMLButtonElement,
    );
    expect(
      await screen.findByText(/Exported to \/h\/coder\/backups\/p\.zip \(no keys/),
    ).toBeTruthy();
    const call = api.calls.find((c) => c.path === "/api/profiles/coder/export");
    expect(call?.body).toBeUndefined(); // the server decides what to leave out; nothing is sent
    expect(
      screen.getByText("Exported archives never contain keys; add them again after an import."),
    ).toBeTruthy();
  });

  it("delete needs the typed name and runs through the switcher", async () => {
    const api = backend();
    const fake = changes();
    render(<ProfilesOverlay api={api} onClose={() => {}} changes={fake} />);
    const card = await coderCard();
    fireEvent.click(
      Array.from(card.querySelectorAll("button")).find(
        (b) => b.textContent === "Delete",
      ) as HTMLButtonElement,
    );
    const confirm = screen.getByText("Delete for good") as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);
    fireEvent.change(
      screen.getByLabelText("Type coder to delete it with its sessions and settings"),
      {
        target: { value: "code" },
      },
    );
    expect(confirm.disabled).toBe(true);
    fireEvent.change(
      screen.getByLabelText("Type coder to delete it with its sessions and settings"),
      {
        target: { value: "coder" },
      },
    );
    fireEvent.click(confirm);
    await waitFor(() => expect(fake.runs).toEqual(["delete coder"]));
    expect(api.calls.some((c) => c.method === "DELETE" && c.path === "/api/profiles/coder")).toBe(
      true,
    );
  });

  it("a missing persona file is explained and created on request", async () => {
    const api = backend();
    render(<ProfilesOverlay api={api} onClose={() => {}} changes={changes()} />);
    const card = await coderCard();
    fireEvent.click(
      Array.from(card.querySelectorAll("button")).find(
        (b) => b.textContent === "Persona",
      ) as HTMLButtonElement,
    );
    expect(await screen.findByText(/has no persona file yet/)).toBeTruthy();
    fireEvent.click(screen.getByText("Create persona"));
    await waitFor(() => expect(api.calls.some((c) => c.method === "PUT")).toBe(true));
    expect(api.calls.find((c) => c.method === "PUT")?.body).toEqual({ content: "" });
  });
});
