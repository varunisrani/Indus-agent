// @vitest-environment jsdom
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { IndusDesktopBridge } from "../../global.d.ts";
import { FilesPane, PATH_DRAG_TYPE } from "./FilesPane.tsx";
import {
  dirnameOf,
  joinPath,
  localSource,
  remoteSource,
  separatorOf,
  validName,
} from "./source.ts";

type Entry = {
  name: string;
  path: string;
  kind: "file" | "dir";
  size?: number;
  sensitive?: boolean;
};

function fakeFs(tree: Record<string, Entry[]>) {
  const changed = new Set<(change: { watchId: string; path: string; kind: string }) => void>();
  let n = 0;
  const fs = {
    setRoots: vi.fn(async ({ roots }: { roots: string[] }) => ({ roots, refused: [] })),
    readDir: vi.fn(async ({ path }: { path: string }) => {
      const entries = tree[path];
      if (entries === undefined)
        throw new Error("outside-roots: that path is outside this window's folders");
      return { path, entries, truncated: false };
    }),
    rename: vi.fn(async ({ to }: { from: string; to: string }) => ({ path: to })),
    trash: vi.fn(async () => ({ ok: true as const })),
    reveal: vi.fn(async () => ({ ok: true as const })),
    watch: vi.fn(async () => ({ watchId: `w${++n}` })),
    unwatch: vi.fn(async () => ({ ok: true as const })),
    onChanged: vi.fn(
      (listener: (change: { watchId: string; path: string; kind: string }) => void) => {
        changed.add(listener);
        return () => changed.delete(listener);
      },
    ),
  };
  const emit = (watchId: string) => {
    for (const l of [...changed]) l({ watchId, path: "/x", kind: "change" });
  };
  return { fs, emit, bridge: { fs } as unknown as Pick<IndusDesktopBridge, "fs"> };
}

const WS = "/home/me/proj";
const TREE: Record<string, Entry[]> = {
  [WS]: [
    { name: "src", path: `${WS}/src`, kind: "dir" },
    { name: "README.md", path: `${WS}/README.md`, kind: "file", size: 10 },
    { name: ".env", path: `${WS}/.env`, kind: "file", sensitive: true },
  ],
  [`${WS}/src`]: [{ name: "app.ts", path: `${WS}/src/app.ts`, kind: "file", size: 3 }],
};

describe("path helpers", () => {
  it("POSIX and Windows paths", () => {
    expect(separatorOf("/a/b")).toBe("/");
    expect(separatorOf("C:\\a\\b")).toBe("\\");
    expect(dirnameOf("/a/b/c.txt")).toBe("/a/b");
    expect(dirnameOf("/c.txt")).toBe("/");
    expect(dirnameOf("C:\\a\\c.txt")).toBe("C:\\a");
    expect(dirnameOf("C:\\c.txt")).toBe("C:\\");
    expect(joinPath("/a", "b")).toBe("/a/b");
    expect(joinPath("C:\\a", "b")).toBe("C:\\a\\b");
    expect(validName("ok.txt")).toBe(true);
    for (const bad of ["", " ", ".", "..", "a/b", "a\\b"]) expect(validName(bad)).toBe(false);
  });
});

describe("FilesPane (local)", () => {
  it("declares the workspace to main, lists it lazily and watches what is open", async () => {
    const { fs, bridge, emit } = fakeFs(TREE);
    const view = render(<FilesPane workspace={WS} mode="local" source={localSource(bridge)} />);
    await screen.findByText("README.md");
    expect(fs.setRoots).toHaveBeenCalledWith({ roots: [WS] });
    expect(fs.readDir).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("app.ts")).toBeNull();
    fireEvent.click(screen.getByText("src"));
    await screen.findByText("app.ts");
    await waitFor(() => expect(fs.watch).toHaveBeenCalledTimes(2));
    expect(fs.watch).toHaveBeenLastCalledWith({ path: `${WS}/src` });
    // a change in the root folder re-reads it
    act(() => emit("w1"));
    await waitFor(() => expect(fs.readDir).toHaveBeenCalledTimes(3));
    // collapsing stops its watch; unmounting stops the rest
    fireEvent.click(screen.getByText("src"));
    await waitFor(() => expect(fs.unwatch).toHaveBeenCalledWith({ watchId: "w2" }));
    view.unmount();
    expect(fs.unwatch).toHaveBeenCalledWith({ watchId: "w1" });
  });

  it("a sensitive file is listed with a lock and never previewed", async () => {
    const { bridge } = fakeFs(TREE);
    const onPreview = vi.fn();
    render(
      <FilesPane workspace={WS} mode="local" source={localSource(bridge)} onPreview={onPreview} />,
    );
    fireEvent.click(await screen.findByText(".env"));
    expect(onPreview).not.toHaveBeenCalled();
    expect(screen.getAllByLabelText(/May hold secrets/).length).toBeGreaterThan(0);
    fireEvent.click(screen.getByText("README.md"));
    expect(onPreview).toHaveBeenCalledWith(`${WS}/README.md`);
  });

  it("rename within the workspace goes to main with the new path in the same folder", async () => {
    const { fs, bridge } = fakeFs(TREE);
    render(<FilesPane workspace={WS} mode="local" source={localSource(bridge)} />);
    await screen.findByText("README.md");
    fireEvent.click(screen.getByRole("button", { name: "Actions for README.md" }));
    fireEvent.click(screen.getByRole("button", { name: "Rename" }));
    const field = screen.getByRole("textbox", { name: "New name for README.md" });
    fireEvent.change(field, { target: { value: "../escape.md" } });
    fireEvent.submit(field);
    expect(await screen.findByText(/cannot be empty or contain a slash/)).toBeTruthy();
    expect(fs.rename).not.toHaveBeenCalled();
    fireEvent.change(field, { target: { value: "GUIDE.md" } });
    fireEvent.submit(field);
    await waitFor(() =>
      expect(fs.rename).toHaveBeenCalledWith({ from: `${WS}/README.md`, to: `${WS}/GUIDE.md` }),
    );
  });

  it("trash, reveal, and main's refusal shown as a message", async () => {
    const { fs, bridge } = fakeFs(TREE);
    fs.trash.mockRejectedValueOnce(
      new Error("root-protected: a workspace or home folder cannot be changed here"),
    );
    render(<FilesPane workspace={WS} mode="local" source={localSource(bridge)} />);
    await screen.findByText("README.md");
    fireEvent.click(screen.getByRole("button", { name: "Actions for README.md" }));
    fireEvent.click(screen.getByRole("button", { name: "Move to trash" }));
    expect(await screen.findByText(/cannot be changed here/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Move to trash" }));
    await waitFor(() => expect(fs.trash).toHaveBeenCalledTimes(2));
    fireEvent.click(screen.getByRole("button", { name: "Actions for src" }));
    fireEvent.click(screen.getByRole("button", { name: "Show in folder" }));
    expect(fs.reveal).toHaveBeenCalledWith({ path: `${WS}/src` });
  });

  it("rows drag as a path for the composer", async () => {
    const { bridge } = fakeFs(TREE);
    render(<FilesPane workspace={WS} mode="local" source={localSource(bridge)} />);
    const row = (await screen.findByText("README.md")).closest("[draggable]");
    if (row === null) throw new Error("no draggable row");
    const data = new Map<string, string>();
    fireEvent.dragStart(row, {
      dataTransfer: { setData: (k: string, v: string) => data.set(k, v), effectAllowed: "" },
    });
    expect(data.get("text/plain")).toBe(`${WS}/README.md`);
    expect(JSON.parse(data.get(PATH_DRAG_TYPE) ?? "{}")).toEqual({
      path: `${WS}/README.md`,
      remote: false,
    });
  });

  it("a folder main refuses shows the reason", async () => {
    const { bridge } = fakeFs({});
    render(<FilesPane workspace="/etc" mode="local" source={localSource(bridge)} />);
    expect((await screen.findByRole("alert")).textContent).toMatch(/outside this window's folders/);
  });

  it("no working folder: says so", () => {
    render(<FilesPane workspace={null} mode="local" source={null} />);
    expect(screen.getByText(/no working folder/)).toBeTruthy();
  });
});

describe("FilesPane (remote)", () => {
  function fakeApi() {
    const request = vi.fn(async (req: { method: string; path: string }) => {
      if (
        req.method === "GET" &&
        req.path === `/api/files?path=${encodeURIComponent("/srv/app")}`
      ) {
        return {
          kind: "response" as const,
          status: 200,
          body: {
            path: "/srv/app",
            entries: [
              { name: "data", path: "/srv/app/data", is_directory: true, size: null },
              { name: "main.py", path: "/srv/app/main.py", is_directory: false, size: 20 },
            ],
          },
        };
      }
      if (req.method === "DELETE")
        return { kind: "response" as const, status: 200, body: { ok: true } };
      return {
        kind: "response" as const,
        status: 403,
        body: { detail: { code: "x", message: "That folder cannot be read." } },
      };
    });
    return { request, bridge: { api: { request } } as unknown as Pick<IndusDesktopBridge, "api"> };
  }

  it("lists through the REST proxy; no rename, reveal or local preview; delete asks first", async () => {
    const { request, bridge } = fakeApi();
    const onPreview = vi.fn();
    render(
      <FilesPane
        workspace="/srv/app"
        mode="remote"
        source={remoteSource(bridge)}
        onPreview={onPreview}
      />,
    );
    await screen.findByText("main.py");
    expect(screen.getByText(/On the remote computer/)).toBeTruthy();
    fireEvent.click(screen.getByText("main.py"));
    expect(onPreview).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Actions for data" }));
    expect(screen.queryByRole("button", { name: "Rename" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Show in folder" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    const dialog = screen.getByRole("alertdialog");
    expect(dialog.textContent).toMatch(/Delete data on the remote computer\?/);
    expect(request).toHaveBeenCalledTimes(1);
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete" }));
    await waitFor(() =>
      expect(request).toHaveBeenCalledWith({
        method: "DELETE",
        path: `/api/files?path=${encodeURIComponent("/srv/app/data")}&recursive=true`,
      }),
    );
  });

  it("a REST refusal shows the server's message", async () => {
    const { bridge } = fakeApi();
    render(<FilesPane workspace="/root" mode="remote" source={remoteSource(bridge)} />);
    expect((await screen.findByRole("alert")).textContent).toMatch(/That folder cannot be read\./);
  });
});

describe("FilesPane look (W52-m)", () => {
  it("tints changed files from git status and collapses every open folder", async () => {
    const { bridge } = fakeFs(TREE);
    const git = {
      status: vi.fn(async () => ({
        root: WS,
        branch: "main",
        upstream: null,
        ahead: 0,
        behind: 0,
        head: null,
        truncated: false,
        files: [
          { path: "README.md", index: " ", worktree: "M" },
          { path: "src/app.ts", index: "?", worktree: "?" },
        ],
      })),
    };
    render(<FilesPane workspace={WS} mode="local" source={localSource(bridge)} git={git} />);
    const readme = await screen.findByText("README.md");
    await waitFor(() =>
      expect(readme.closest("[data-tint]")?.getAttribute("data-tint")).toBe("modified"),
    );
    expect(screen.queryByRole("button", { name: "Collapse all" })).toBeNull();
    fireEvent.click(screen.getByText("src"));
    const app = await screen.findByText("app.ts");
    expect(app.closest("[data-tint]")?.getAttribute("data-tint")).toBe("added");
    fireEvent.click(screen.getByRole("button", { name: "Collapse all" }));
    expect(screen.queryByText("app.ts")).toBeNull();
  });

  it("a right click opens the row's actions; an empty folder shows a quiet label", async () => {
    const { bridge } = fakeFs({ ...TREE, [`${WS}/src`]: [] });
    render(<FilesPane workspace={WS} mode="local" source={localSource(bridge)} git={null} />);
    fireEvent.contextMenu(await screen.findByText("README.md"));
    expect(screen.getByRole("button", { name: "Copy path" })).toBeTruthy();
    fireEvent.click(screen.getByText("src"));
    expect(await screen.findByText("This folder is empty.")).toBeTruthy();
  });
});
