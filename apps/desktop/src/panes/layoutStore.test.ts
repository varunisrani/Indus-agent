import { describe, expect, it } from "vitest";
import { LAYOUT_KEY, LayoutStore, type StorageLike, withoutUnknown } from "./layoutStore.ts";
import { allPanes, findZone, layoutProblems, zoneOf } from "./paneTree.ts";
import { surfaceShown } from "./presets.ts";

const ALL = ["chat", "preview", "files", "review", "terminal"];

function memoryStorage(): StorageLike & { data: Map<string, string> } {
  const data = new Map<string, string>();
  return {
    data,
    getItem: (key) => data.get(key) ?? null,
    setItem: (key, value) => {
      data.set(key, value);
    },
    removeItem: (key) => {
      data.delete(key);
    },
  };
}

function store(storage: StorageLike | null = memoryStorage(), available = ALL) {
  return new LayoutStore({ storage, available: () => available });
}

describe("layout store", () => {
  it("starts from the Default preset in Advanced mode", () => {
    const s = store();
    expect(s.mode()).toBe("advanced");
    expect(new Set(allPanes(s.layout().root))).toEqual(new Set(ALL));
    expect(layoutProblems(s.layout())).toEqual([]);
  });

  it("Simple mode hides terminal, files and review and restores Advanced on the way back", () => {
    const s = store();
    s.move("terminal", { zoneId: zoneOf(s.layout().root, "chat")?.id ?? "", edge: "bottom" });
    s.setMinimized(zoneOf(s.layout().root, "files")?.id ?? "", true);
    const advanced = s.layout();
    s.setMode("simple");
    const simple = allPanes(s.layout().root);
    expect(simple).not.toContain("terminal");
    expect(simple).not.toContain("files");
    expect(simple).not.toContain("review");
    expect(s.open("terminal")).toBe(false);
    expect(allPanes(s.layout().root)).not.toContain("terminal");
    s.open("preview");
    s.setMode("advanced");
    expect(s.layout()).toBe(advanced);
  });

  it("Simple surfaces: status bar, tool details, inline diffs hidden; the rail only with several profiles", () => {
    for (const surface of [
      "statusBar",
      "toolDetails",
      "inlineDiffs",
      "thinkingExpanded",
    ] as const) {
      expect(surfaceShown("simple", surface, { profileCount: 3 })).toBe(false);
      expect(surfaceShown("advanced", surface, { profileCount: 1 })).toBe(true);
    }
    expect(surfaceShown("simple", "profileRail", { profileCount: 1 })).toBe(false);
    expect(surfaceShown("simple", "profileRail", { profileCount: 2 })).toBe(true);
  });

  it("is persisted per mode and read back after a reload", () => {
    const storage = memoryStorage();
    const first = store(storage);
    first.close("review");
    first.setMode("simple");
    first.applyPreset("focus");
    const second = store(storage);
    expect(second.mode()).toBe("simple");
    expect(second.getSnapshot().preset).toBe("focus");
    second.setMode("advanced");
    expect(allPanes(second.layout().root)).not.toContain("review");
    expect(second.layout().closed.map((c) => c.pane)).toContain("review");
  });

  it("survives broken storage and a corrupt record", () => {
    const throwing: StorageLike = {
      getItem: () => {
        throw new Error("denied");
      },
      setItem: () => {
        throw new Error("full");
      },
      removeItem: () => undefined,
    };
    const s = store(throwing);
    s.close("files");
    expect(allPanes(s.layout().root)).not.toContain("files");
    const corrupt = memoryStorage();
    corrupt.setItem(LAYOUT_KEY, "{not json");
    expect(layoutProblems(store(corrupt).layout())).toEqual([]);
  });

  it("puts the chat back when a stored layout lost it", () => {
    const storage = memoryStorage();
    storage.setItem(
      LAYOUT_KEY,
      JSON.stringify({
        version: 1,
        mode: "advanced",
        layouts: {
          advanced: {
            version: 1,
            root: { kind: "zone", id: "z1", tabs: ["preview"], active: "preview" },
            hidden: [],
            closed: [],
          },
        },
      }),
    );
    expect(allPanes(store(storage).layout().root)).toEqual(["chat", "preview"]);
  });

  it("reopen last closed and the focused zone for cycle and jump", () => {
    const s = store();
    const docs = zoneOf(s.layout().root, "preview")?.id ?? "";
    s.setFocusedZone(docs);
    s.jump(2);
    expect(findZone(s.layout().root, docs)?.active).toBe("files");
    s.cycle(1);
    expect(findZone(s.layout().root, docs)?.active).toBe("review");
    s.close("review");
    expect(s.reopenLast()).toBe("review");
    expect(s.reopenLast()).toBeNull();
  });

  it("notifies subscribers with a stable snapshot between changes", () => {
    const s = store();
    let calls = 0;
    const unsubscribe = s.subscribe(() => {
      calls += 1;
    });
    const before = s.getSnapshot();
    expect(s.getSnapshot()).toBe(before);
    s.setHidden("terminal", true);
    expect(calls).toBe(1);
    expect(s.getSnapshot()).not.toBe(before);
    unsubscribe();
    s.setHidden("terminal", false);
    expect(calls).toBe(1);
  });

  it("the rendered view leaves out panes nothing registered", () => {
    const s = store();
    const view = withoutUnknown(s.layout(), new Set(["chat", "preview"]));
    expect(new Set(allPanes(view.root))).toEqual(new Set(["chat", "preview"]));
  });
});
