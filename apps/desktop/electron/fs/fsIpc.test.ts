import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FakeContents, type PaneIpcHarness, paneIpcHarness } from "../../testing/paneIpc.ts";
import {
  type RawListener,
  WATCH_DEBOUNCE_MS,
  WatchHub,
  type WatchTimers,
} from "../preview/fileWatch.ts";
import {
  ATTACH_MAX_BYTES,
  clampDataUrlMb,
  DATA_URL_DEFAULT_MB,
  FsLimits,
  installFsIpc,
  READ_DIR_MAX_ENTRIES,
} from "./fsIpc.ts";
import { PathGuard } from "./pathGuard.ts";

const posix = process.platform !== "win32";
let base = "";
let home = "";
let ws = "";
let h: PaneIpcHarness;
let trashed: string[] = [];
let revealed: string[] = [];
let dialogAnswer: { canceled: boolean; paths: string[] } = { canceled: true, paths: [] };
let dialogRequests: unknown[] = [];
let rawWatchers: Array<{ dir: string; listener: RawListener; closed: boolean }> = [];
let clock = 0;
let timers: Array<{ at: number; fn: () => void; id: number }> = [];

const fakeTimers: WatchTimers = {
  setTimeout: (fn, ms) => {
    const id = timers.length + 1 + Math.random();
    timers.push({ at: clock + ms, fn, id });
    return id;
  },
  clearTimeout: (handle) => {
    timers = timers.filter((t) => t.id !== handle);
  },
  setInterval: () => 0,
  clearInterval: () => {},
  now: () => clock,
};

function advance(ms: number) {
  clock += ms;
  for (;;) {
    const due = timers.filter((t) => t.at <= clock).sort((a, b) => a.at - b.at)[0];
    if (due === undefined) break;
    timers = timers.filter((t) => t !== due);
    due.fn();
  }
}

beforeEach(async () => {
  base = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "indus-fsipc-")));
  home = path.join(base, "home");
  ws = path.join(base, "ws");
  fs.mkdirSync(home, { recursive: true });
  fs.mkdirSync(path.join(ws, "src"), { recursive: true });
  fs.mkdirSync(path.join(ws, "node_modules"));
  fs.writeFileSync(path.join(ws, "b.txt"), "bee");
  fs.writeFileSync(path.join(ws, "A.md"), "# a");
  fs.writeFileSync(path.join(ws, "id_ed25519"), "not a real key");
  trashed = [];
  revealed = [];
  dialogRequests = [];
  rawWatchers = [];
  clock = 0;
  timers = [];
  h = paneIpcHarness([11, 12]);
  const guard = new PathGuard({ home });
  installFsIpc({
    registry: h.registry,
    guard,
    watches: new WatchHub({
      timers: fakeTimers,
      newId: (() => {
        let n = 0;
        return () => `w${++n}`;
      })(),
      fs: {
        watch: (dir, listener) => {
          const entry = { dir, listener, closed: false };
          rawWatchers.push(entry);
          return { close: () => (entry.closed = true) };
        },
        snapshot: () => "",
        exists: (p) => fs.existsSync(p),
      },
    }),
    limits: new FsLimits(path.join(base, "userData", "fs-limits.json")),
    contents: (id) => h.contents(id),
    trashItem: async (file) => {
      trashed.push(file);
    },
    showItemInFolder: (file) => revealed.push(file),
    showDialog: async (_owner, request) => {
      dialogRequests.push(request);
      return dialogAnswer;
    },
    log: () => {},
  });
  await h.invoke("indus:fs:set-roots", { roots: [ws] });
});

afterEach(() => {
  fs.rmSync(base, { recursive: true, force: true });
});

async function code(promise: Promise<unknown>): Promise<string> {
  try {
    await promise;
    return "resolved";
  } catch (error) {
    return String((error as Error).message).split(":")[0] ?? "";
  }
}

describe("fs IPC: confinement", () => {
  it("../../etc/passwd and friends are refused", async () => {
    expect(await code(h.invoke("indus:fs:read-text", { path: "../../etc/passwd" }))).toBe(
      "invalid-path",
    );
    expect(
      await code(h.invoke("indus:fs:read-text", { path: `${ws}/../../../../etc/passwd` })),
    ).toBe("outside-roots");
    expect(await code(h.invoke("indus:fs:read-dir", { path: "/etc" }))).toBe("outside-roots");
    // window 12 never declared the workspace
    expect(await code(h.invoke("indus:fs:read-dir", { path: ws }, 12))).toBe("outside-roots");
  });

  it("id_ed25519 read refused (text, data URL, stat, preview watch); listed with a lock", async () => {
    const key = path.join(ws, "id_ed25519");
    expect(await code(h.invoke("indus:fs:read-text", { path: key }))).toBe("sensitive-file");
    expect(await code(h.invoke("indus:fs:read-data-url", { path: key, purpose: "attach" }))).toBe(
      "sensitive-file",
    );
    expect(await code(h.invoke("indus:fs:stat", { path: key, purpose: "attach" }))).toBe(
      "sensitive-file",
    );
    const listing = (await h.invoke("indus:fs:read-dir", { path: ws })) as {
      entries: Array<{ name: string; sensitive?: boolean }>;
    };
    expect(listing.entries.find((e) => e.name === "id_ed25519")?.sensitive).toBe(true);
  });

  it("a malformed payload never reaches the handler", async () => {
    expect(await code(h.invoke("indus:fs:read-text", { path: ws, extra: 1 }))).toBe("bad-request");
    expect(await code(h.invoke("indus:fs:set-roots", { roots: "x" }))).toBe("bad-request");
  });

  it("a closed window loses its roots", async () => {
    h.window(11).destroy();
    h.windows.set(11, new FakeContents(11));
    expect(await code(h.invoke("indus:fs:read-dir", { path: ws }))).toBe("outside-roots");
  });
});

describe("fs IPC: reads", () => {
  it("lists folders first, by name, without noise", async () => {
    const listing = (await h.invoke("indus:fs:read-dir", { path: ws })) as {
      entries: Array<{ name: string; kind: string; size?: number }>;
      truncated: boolean;
    };
    expect(listing.entries.map((e) => e.name)).toEqual(["src", "A.md", "b.txt", "id_ed25519"]);
    expect(listing.entries[0]?.kind).toBe("dir");
    expect(listing.entries.find((e) => e.name === "b.txt")?.size).toBe(3);
    expect(listing.truncated).toBe(false);
  });

  it(`stops at ${READ_DIR_MAX_ENTRIES} entries`, async () => {
    const big = path.join(ws, "big");
    fs.mkdirSync(big);
    for (let i = 0; i < READ_DIR_MAX_ENTRIES + 3; i += 1)
      fs.writeFileSync(path.join(big, `f${i}`), "");
    const listing = (await h.invoke("indus:fs:read-dir", { path: big })) as {
      entries: unknown[];
      truncated: boolean;
    };
    expect(listing.entries).toHaveLength(READ_DIR_MAX_ENTRIES);
    expect(listing.truncated).toBe(true);
  });

  it("reads text up to the cap and says when it is cut or binary", async () => {
    const big = path.join(ws, "big.txt");
    fs.writeFileSync(big, `${"é".repeat(300 * 1024)}`); // 600 KiB of two-byte characters
    const answer = (await h.invoke("indus:fs:read-text", { path: big })) as {
      text: string;
      truncated: boolean;
      size: number;
      binary: boolean;
    };
    expect(answer.truncated).toBe(true);
    expect(answer.size).toBe(600 * 1024);
    expect(answer.text.length).toBe(256 * 1024);
    expect(answer.text.includes("�")).toBe(false);
    fs.writeFileSync(path.join(ws, "bin.dat"), Buffer.from([1, 0, 2, 3]));
    expect(await h.invoke("indus:fs:read-text", { path: path.join(ws, "bin.dat") })).toMatchObject({
      binary: true,
      text: "",
    });
    expect(
      await h.invoke("indus:fs:read-text", { path: path.join(ws, "b.txt"), maxBytes: 2 }),
    ).toMatchObject({ text: "be", truncated: true });
  });

  it("data URLs: preview capped by the Settings value, attach at 256 MiB", async () => {
    fs.writeFileSync(path.join(ws, "img.png"), Buffer.from([137, 80, 78, 71]));
    expect(
      await h.invoke("indus:fs:read-data-url", {
        path: path.join(ws, "img.png"),
        purpose: "preview",
      }),
    ).toEqual({ dataUrl: "data:image/png;base64,iVBORw==", size: 4, mimeType: "image/png" });
    const two = path.join(ws, "two-mib.bin");
    fs.writeFileSync(two, Buffer.alloc(2 * 1024 * 1024));
    await h.invoke("indus:fs:limits:set", { dataUrlMaxMb: 1 });
    expect(await code(h.invoke("indus:fs:read-data-url", { path: two, purpose: "preview" }))).toBe(
      "too-large",
    );
    expect(
      (
        (await h.invoke("indus:fs:read-data-url", { path: two, purpose: "attach" })) as {
          size: number;
        }
      ).size,
    ).toBe(2 * 1024 * 1024);
  });

  it("300 MiB attach refused (stat and data URL)", async () => {
    const huge = path.join(ws, "huge.bin");
    fs.closeSync(fs.openSync(huge, "w"));
    fs.truncateSync(huge, 300 * 1024 * 1024); // sparse
    expect(await code(h.invoke("indus:fs:stat", { path: huge, purpose: "attach" }))).toBe(
      "too-large",
    );
    expect(await code(h.invoke("indus:fs:read-data-url", { path: huge, purpose: "attach" }))).toBe(
      "too-large",
    );
    const fine = (await h.invoke("indus:fs:stat", {
      path: path.join(ws, "b.txt"),
      purpose: "attach",
    })) as {
      size: number;
      mimeType: string;
    };
    expect(fine).toMatchObject({ size: 3, mimeType: "text/plain", kind: "file" });
    expect(ATTACH_MAX_BYTES).toBe(256 * 1024 * 1024);
  });

  it("limits are clamped and persisted", async () => {
    expect(clampDataUrlMb("x")).toBe(DATA_URL_DEFAULT_MB);
    expect(clampDataUrlMb(0)).toBe(1);
    expect(clampDataUrlMb(10_000)).toBe(4_096);
    expect(await h.invoke("indus:fs:limits:get", {})).toMatchObject({ dataUrlMaxMb: 16 });
    expect(await h.invoke("indus:fs:limits:set", { dataUrlMaxMb: 64.4 })).toMatchObject({
      dataUrlMaxMb: 64,
    });
    expect(new FsLimits(path.join(base, "userData", "fs-limits.json")).view().dataUrlMaxMb).toBe(
      64,
    );
  });
});

describe("fs IPC: changes", () => {
  it("rename within the workspace works and never overwrites", async () => {
    const from = path.join(ws, "b.txt");
    const to = path.join(ws, "src", "c.txt");
    expect(await h.invoke("indus:fs:rename", { from, to })).toEqual({ path: to });
    expect(fs.readFileSync(to, "utf8")).toBe("bee");
    expect(await code(h.invoke("indus:fs:rename", { from: path.join(ws, "A.md"), to }))).toBe(
      "exists",
    );
    expect(fs.readFileSync(to, "utf8")).toBe("bee");
    expect(
      await code(h.invoke("indus:fs:rename", { from: to, to: path.join(base, "escaped.txt") })),
    ).toBe("outside-roots");
    expect(await code(h.invoke("indus:fs:rename", { from: ws, to: path.join(home, "x") }))).toBe(
      "root-protected",
    );
  });

  it("trash and reveal stay inside the roots and never take a root", async () => {
    expect(await h.invoke("indus:fs:trash", { path: path.join(ws, "b.txt") })).toEqual({
      ok: true,
    });
    expect(trashed).toEqual([path.join(ws, "b.txt")]);
    expect(await code(h.invoke("indus:fs:trash", { path: ws }))).toBe("root-protected");
    expect(await code(h.invoke("indus:fs:trash", { path: home }))).toBe("root-protected");
    expect(await code(h.invoke("indus:fs:trash", { path: "/etc/hosts" }))).toBe("outside-roots");
    await h.invoke("indus:fs:reveal", { path: path.join(ws, "A.md") });
    expect(revealed).toEqual([path.join(ws, "A.md")]);
  });

  it("dialog picks are local paths granted to the asking window only", async () => {
    const outside = path.join(base, "picked.txt");
    fs.writeFileSync(outside, "p");
    dialogAnswer = { canceled: false, paths: [outside] };
    expect(
      await h.invoke("indus:fs:select-paths", {
        mode: "open-file",
        multiple: true,
        defaultPath: "/remote/host/only",
      }),
    ).toEqual({ paths: [outside], canceled: false });
    // a path that does not exist here (a remote one) does not seed the dialog
    expect(dialogRequests[0]).toEqual({ mode: "open-file", multiple: true });
    expect(await h.invoke("indus:fs:stat", { path: outside, purpose: "attach" })).toMatchObject({
      size: 1,
    });
    expect(await code(h.invoke("indus:fs:stat", { path: outside, purpose: "attach" }, 12))).toBe(
      "outside-roots",
    );
  });

  it.runIf(posix)("watches a folder: debounced, owned, closed with the window", async () => {
    const { watchId } = (await h.invoke("indus:fs:watch", { path: ws })) as { watchId: string };
    expect(rawWatchers).toHaveLength(1);
    const raw = rawWatchers[0];
    if (raw === undefined) throw new Error("no watcher");
    raw.listener("rename", "new.txt");
    advance(50);
    raw.listener("change", "b.txt");
    advance(WATCH_DEBOUNCE_MS - 1);
    expect(h.window(11).events("indus:fs:changed")).toEqual([]);
    advance(1);
    expect(h.window(11).events("indus:fs:changed")).toEqual([
      { watchId, path: path.join(ws, "b.txt"), kind: "change" },
    ]);
    // only the owner may unwatch
    await h.invoke("indus:fs:unwatch", { watchId }, 12);
    expect(raw.closed).toBe(false);
    h.window(11).destroy();
    expect(raw.closed).toBe(true);
  });

  it("refuses a watch outside the roots or on a file", async () => {
    expect(await code(h.invoke("indus:fs:watch", { path: "/etc" }))).toBe("outside-roots");
    expect(await code(h.invoke("indus:fs:watch", { path: path.join(ws, "b.txt") }))).toBe(
      "not-a-directory",
    );
    expect(vi.isMockFunction(fs.watch)).toBe(false);
  });
});
