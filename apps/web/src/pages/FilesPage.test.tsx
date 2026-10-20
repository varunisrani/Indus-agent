import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createMockApi, type MockApi } from "../testing/mockApi.ts";
import { renderPage } from "../testing/render.tsx";
import { FilesPage } from "./FilesPage.tsx";

let mock: MockApi;
let restore: () => void;
const fakeToken = () => ["session", "tok", "for", "tests"].join("/");

beforeEach(() => {
  mock = createMockApi();
  mock.state.files = {
    "/home/user": [
      {
        name: "notes.txt",
        path: "/home/user/notes.txt",
        is_directory: false,
        size: 12,
        mtime: 1_760_000_000,
      },
      {
        name: "projects",
        path: "/home/user/projects",
        is_directory: true,
        size: null,
        mtime: null,
      },
    ],
    "/home/user/projects": [],
    "/home": [{ name: "user", path: "/home/user", is_directory: true, size: null, mtime: null }],
  };
  mock.state.fileText = { "/home/user/notes.txt": "hello there" };
  restore = mock.install();
  vi.spyOn(console, "warn").mockImplementation(() => {});
});

afterEach(() => {
  restore();
});

describe("files", () => {
  it("downloads through a minted ticket, never the session token in the URL", async () => {
    window.__INDUS_SESSION_TOKEN__ = fakeToken();
    window.__INDUS_BASE_PATH__ = "/indus";
    const opened: string[] = [];
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      opened.push(this.getAttribute("href") ?? "");
    });
    renderPage(<FilesPage />, "/files");
    fireEvent.click(await screen.findByRole("button", { name: "Download notes.txt" }));
    await waitFor(() => expect(opened).toHaveLength(1));
    const minted = mock.callsTo("POST", "/api/files/download-ticket");
    expect(minted).toHaveLength(1);
    expect(minted[0]?.body).toEqual({ path: "/home/user/notes.txt" });
    const href = new URL(opened[0] ?? "", "http://localhost");
    expect(href.pathname).toBe("/indus/api/files/download");
    expect(href.searchParams.get("path")).toBe("/home/user/notes.txt");
    expect(href.searchParams.get("ticket")).toBe("ticket-for-/home/user/notes.txt");
    expect(href.searchParams.has("token")).toBe(false);
    expect(opened[0]).not.toContain(fakeToken());
  });

  it("mints a ticket in gated mode too (the cookie alone is not enough)", async () => {
    window.__INDUS_AUTH_REQUIRED__ = true;
    const opened: string[] = [];
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      opened.push(this.getAttribute("href") ?? "");
    });
    renderPage(<FilesPage />, "/files");
    fireEvent.click(await screen.findByRole("button", { name: "Download notes.txt" }));
    await waitFor(() => expect(opened).toHaveLength(1));
    expect(opened[0]).toContain("ticket=");
    expect(opened[0]).not.toContain("token=");
  });

  it("asks before deleting", async () => {
    renderPage(<FilesPage />, "/files");
    fireEvent.click(await screen.findByRole("button", { name: "Delete notes.txt" }));
    const dialog = await screen.findByRole("dialog");
    expect(mock.callsTo("DELETE", "/api/files")).toHaveLength(0);
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(mock.callsTo("DELETE", "/api/files")).toHaveLength(0);

    fireEvent.click(screen.getByRole("button", { name: "Delete notes.txt" }));
    fireEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Delete" }),
    );
    await waitFor(() => expect(mock.callsTo("DELETE", "/api/files")).toHaveLength(1));
    expect(mock.callsTo("DELETE", "/api/files")[0]?.query.get("path")).toBe("/home/user/notes.txt");
    await waitFor(() => expect(screen.queryByTestId("file-notes.txt")).toBeNull());
  });

  it("makes a folder in the current directory and refuses path tricks", async () => {
    renderPage(<FilesPage />, "/files");
    const name = await screen.findByLabelText("New folder name");
    fireEvent.change(name, { target: { value: "../escape" } });
    fireEvent.click(screen.getByRole("button", { name: "Create folder" }));
    expect(mock.callsTo("POST", "/api/files/mkdir")).toHaveLength(0);
    fireEvent.change(name, { target: { value: "drafts" } });
    fireEvent.click(screen.getByRole("button", { name: "Create folder" }));
    await waitFor(() =>
      expect(mock.callsTo("POST", "/api/files/mkdir")[0]?.body).toEqual({
        path: "/home/user/drafts",
      }),
    );
    expect(await screen.findByTestId("file-drafts")).toBeTruthy();
  });

  it("opens folders, goes up and previews text", async () => {
    renderPage(<FilesPage />, "/files");
    fireEvent.click(await screen.findByRole("button", { name: "projects/" }));
    await waitFor(() =>
      expect(screen.getByTestId("current-path").textContent).toBe("/home/user/projects"),
    );
    await screen.findByText("This folder is empty.");
    fireEvent.click(screen.getByRole("button", { name: "↑ Up" }));
    await waitFor(() => expect(screen.getByTestId("current-path").textContent).toBe("/home/user"));
    fireEvent.click(await screen.findByRole("button", { name: "notes.txt" }));
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText("hello there")).toBeTruthy();
  });

  it("uploads into the current folder", async () => {
    renderPage(<FilesPage />, "/files");
    await screen.findByTestId("file-notes.txt");
    const file = new File(["data"], "report.csv", { type: "text/csv" });
    fireEvent.change(screen.getByLabelText("Upload files"), { target: { files: [file] } });
    await waitFor(() => expect(mock.callsTo("POST", "/api/files/upload")).toHaveLength(1));
    const body = mock.callsTo("POST", "/api/files/upload")[0]?.body as Record<string, unknown>;
    expect(body.path).toBe("/home/user/report.csv");
    expect(await screen.findByText("1 file uploaded.")).toBeTruthy();
  });
});
