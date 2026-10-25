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

async function selectCoder(): Promise<void> {
  await waitFor(() => expect(document.querySelector("[data-profile='coder']")).not.toBeNull());
  const row = document.querySelector("[data-profile='coder'] button") as HTMLButtonElement;
  fireEvent.click(row);
  await waitFor(() => expect(document.querySelector("[data-detail='coder']")).not.toBeNull());
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
    await selectCoder();
    fireEvent.click(screen.getByRole("button", { name: "Export" }));
    expect(
      await screen.findByText(/Exported to \/h\/coder\/backups\/p\.zip \(no keys/),
    ).toBeTruthy();
    const call = api.calls.find((c) => c.path === "/api/profiles/coder/export");
    expect(call?.body).toBeUndefined(); // the server decides what to leave out; nothing is sent
    fireEvent.click(screen.getByRole("button", { name: "Import" }));
    expect(
      await screen.findByText(
        "Exported archives never contain keys; add them again after an import.",
      ),
    ).toBeTruthy();
  });

  it("delete needs the typed name and runs through the switcher", async () => {
    const api = backend();
    const fake = changes();
    render(<ProfilesOverlay api={api} onClose={() => {}} changes={fake} />);
    await selectCoder();
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    const confirm = (await screen.findByText("Delete for good")) as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);
    fireEvent.change(
      screen.getByLabelText("Type coder to delete it with its sessions and settings"),
      { target: { value: "code" } },
    );
    expect(confirm.disabled).toBe(true);
    fireEvent.change(
      screen.getByLabelText("Type coder to delete it with its sessions and settings"),
      { target: { value: "coder" } },
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
    await selectCoder();
    expect(await screen.findByText(/has no persona file yet/)).toBeTruthy();
    fireEvent.click(screen.getByText("Create persona"));
    await waitFor(() => expect(api.calls.some((c) => c.method === "PUT")).toBe(true));
    expect(api.calls.find((c) => c.method === "PUT")?.body).toEqual({ content: "" });
  });

  it("the default profile shows its pill and no delete", async () => {
    const api = backend();
    render(<ProfilesOverlay api={api} onClose={() => {}} changes={changes()} />);
    await waitFor(() => expect(document.querySelector("[data-profile='default']")).not.toBeNull());
    fireEvent.click(document.querySelector("[data-profile='default'] button") as HTMLElement);
    expect(await screen.findByText("Default")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Delete" })).toBeNull();
    expect(screen.getByText("2 profiles")).toBeTruthy();
  });
});
