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

/** A store laid out with every pane (the Default preset); `fresh` keeps the start layout. */
function store(storage: StorageLike | null = memoryStorage(), available = ALL, fresh = false) {
  const s = new LayoutStore({ storage, available: () => available });
  if (!fresh) s.applyPreset("default");
  return s;
}

describe("layout store", () => {
  it("starts on the chat alone in Advanced mode, the tool panes ready to reopen", () => {
    const s = store(memoryStorage(), ALL, true);
    expect(s.mode()).toBe("advanced");
    expect(allPanes(s.layout().root)).toEqual(["chat"]);
    expect(
      s
        .layout()
        .closed.map((c) => c.pane)
        .sort(),
    ).toEqual(ALL.filter((pane) => pane !== "chat").sort());
    expect(layoutProblems(s.layout())).toEqual([]);
    expect(s.reopenLast()).toBe("preview");
    s.applyPreset("default");
    expect(new Set(allPanes(s.layout().root))).toEqual(new Set(ALL));
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
    const second = store(storage, ALL, true);
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
    expect(allPanes(store(storage, ALL, true).layout().root)).toEqual(["chat", "preview"]);
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

describe("the right-side group (W52-m)", () => {
  it("files on top, the terminal under the whole right group, review left of files", () => {
    const s = store(memoryStorage(), ALL, true);
    s.open("files");
    const root1 = s.layout().root;
    expect(root1?.kind === "split" && root1.orientation).toBe("row");
    expect(allPanes(root1)).toEqual(["chat", "files"]);
    s.open("terminal");
    const root2 = s.layout().root;
    expect(root2?.kind).toBe("split");
    const right = root2?.kind === "split" ? root2.children[1] : null;
    expect(right?.kind === "split" && right.orientation).toBe("column");
    expect(allPanes(right ?? null)).toEqual(["files", "terminal"]);
    s.open("review");
    const right3 = (() => {
      const root = s.layout().root;
      return root?.kind === "split" ? root.children[1] : null;
    })();
    expect(allPanes(right3 ?? null)).toEqual(["review", "files", "terminal"]);
    const top = right3?.kind === "split" ? right3.children[0] : null;
    expect(top?.kind === "split" && top.orientation).toBe("row");
    expect(layoutProblems(s.layout())).toEqual([]);
    // closing review takes only its column away
    s.close("review");
    expect(allPanes(s.layout().root)).toEqual(["chat", "files", "terminal"]);
  });

  it("Ctrl+J's Files over Terminal column gets 236 of the 1180 span; Review 380, Preview 420 (W57-H1-2)", () => {
    // The split sizes are relative flex weights: at the 1440 px capture the chat area spans
    // 1143 px, so the column draws at 236 / 1180 × 1143 ≈ 229 px (measured live: 228 px).
    const s = store(memoryStorage(), ALL, true);
    const share = () => {
      const root = s.layout().root;
      if (root?.kind !== "split") return null;
      const total = root.sizes.reduce((sum, size) => sum + size, 0);
      return Math.round((root.sizes[1] ?? 0) * (1180 / total));
    };
    s.open("files");
    s.open("terminal");
    expect(share()).toBe(236);
    const right = (() => {
      const root = s.layout().root;
      return root?.kind === "split" ? root.children[1] : null;
    })();
    expect(right?.kind === "split" && right.orientation).toBe("column");
    s.close("files");
    s.close("terminal");
    s.open("review");
    expect(share()).toBe(380);
    s.close("review");
    s.open("preview");
    expect(share()).toBe(420);
  });

  it("an arrangement the group did not build opens panes the usual way", () => {
    const s = store(); // the Default preset: every pane already placed
    s.close("review");
    s.open("review");
    expect(new Set(allPanes(s.layout().root))).toEqual(new Set(ALL));
    expect(layoutProblems(s.layout())).toEqual([]);
  });
});
