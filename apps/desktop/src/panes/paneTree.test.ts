import { describe, expect, it } from "vitest";
import { seeded } from "../../testing/seeded.ts";
import {
  activate,
  allPanes,
  CHAT_PANE,
  closePane,
  cycleTab,
  type DropEdge,
  emptyLayout,
  findZone,
  insertPane,
  jumpToSlot,
  layoutOf,
  layoutProblems,
  MAX_CLOSED,
  movePane,
  normalize,
  openPane,
  type PaneLayout,
  parseLayout,
  reopenLastClosed,
  resizeSplit,
  setHidden,
  setMinimized,
  split,
  zone,
  zoneOf,
  zones,
} from "./paneTree.ts";
import { applyPreset, initialLayout, PRESET_IDS, presetTree } from "./presets.ts";

const ALL = ["chat", "preview", "files", "review", "terminal"];

function basic(): PaneLayout {
  return layoutOf(split("s1", "row", [zone("z1", ["chat"]), zone("z2", ["preview", "files"])]));
}

describe("normal form", () => {
  it("drops empty zones, unwraps lone children and flattens same-direction splits", () => {
    const tree = split("s1", "row", [
      zone("z1", ["chat"]),
      split("s2", "row", [zone("z2", ["preview"]), zone("z3", [])], [3, 1]),
      split("s3", "column", [zone("z4", ["files"])]),
    ]);
    const root = normalize(tree);
    expect(root?.kind).toBe("split");
    if (root?.kind !== "split") return;
    expect(root.children.map((c) => c.id)).toEqual(["z1", "z2", "z4"]);
    expect(root.sizes).toEqual([1, 1, 1]);
  });

  it("scales flattened sizes into the parent slot", () => {
    const tree = split(
      "s1",
      "row",
      [
        zone("z1", ["chat"]),
        split("s2", "row", [zone("z2", ["preview"]), zone("z3", ["files"])], [3, 1]),
      ],
      [1, 2],
    );
    const root = normalize(tree);
    if (root?.kind !== "split") throw new Error("split expected");
    expect(root.sizes).toEqual([1, 1.5, 0.5]);
  });

  it("an all-empty tree is nothing; a zone keeps a valid active tab", () => {
    expect(normalize(split("s1", "row", [zone("z1", []), zone("z2", [])]))).toBeNull();
    const z = normalize({
      kind: "zone",
      id: "z1",
      tabs: ["a", "a", "b"],
      active: "x",
      minimized: false,
    });
    expect(z).toEqual({ kind: "zone", id: "z1", tabs: ["a", "b"], active: "a", minimized: false });
  });
});

describe("moving and dropping", () => {
  it("a centre drop joins the zone's tabs and activates the pane", () => {
    const next = movePane(basic(), "preview", { zoneId: "z1", edge: "center" });
    expect(zoneOf(next.root, "preview")?.id).toBe("z1");
    expect(findZone(next.root, "z1")?.active).toBe("preview");
    expect(layoutProblems(next)).toEqual([]);
  });

  it.each<[DropEdge, "row" | "column", boolean]>([
    ["left", "row", true],
    ["right", "row", false],
    ["top", "column", true],
    ["bottom", "column", false],
  ])("an edge drop (%s) splits the target zone", (edge, orientation, before) => {
    const next = movePane(basic(), "files", { zoneId: "z1", edge });
    const host = zoneOf(next.root, "files");
    expect(host?.tabs).toEqual(["files"]);
    const parentHasBoth = (node: typeof next.root): boolean => {
      if (node === null || node.kind === "zone") return false;
      const ids = node.children.map((c) => (c.kind === "zone" ? c.tabs[0] : null));
      if (ids.includes("chat") && ids.includes("files")) {
        expect(node.orientation).toBe(orientation);
        expect(ids.indexOf("files") < ids.indexOf("chat")).toBe(before);
        return true;
      }
      return node.children.some(parentHasBoth);
    };
    expect(parentHasBoth(next.root)).toBe(true);
    expect(layoutProblems(next)).toEqual([]);
  });

  it("dragging the last tab out of a zone removes the zone", () => {
    const one = movePane(basic(), "preview", { zoneId: "z1", edge: "center" });
    const two = movePane(one, "files", { zoneId: "z1", edge: "center" });
    expect(zones(two.root).map((z) => z.id)).toEqual(["z1"]);
    expect(two.root?.kind).toBe("zone");
  });

  it("a centre drop on its own zone reorders; an edge drop on its own lone zone does nothing", () => {
    const reordered = movePane(basic(), "files", { zoneId: "z2", edge: "center", index: 0 });
    expect(findZone(reordered.root, "z2")?.tabs).toEqual(["files", "preview"]);
    const lone = layoutOf(split("s1", "row", [zone("z1", ["chat"]), zone("z2", ["preview"])]));
    expect(movePane(lone, "preview", { zoneId: "z2", edge: "left" })).toBe(lone);
  });

  it("a drop on a zone that does not exist changes nothing", () => {
    const layout = basic();
    expect(movePane(layout, "preview", { zoneId: "nope", edge: "center" })).toBe(layout);
  });
});

describe("close, reopen, hide, minimise", () => {
  it("the chat cannot be closed", () => {
    const layout = basic();
    expect(closePane(layout, CHAT_PANE)).toBe(layout);
  });

  it("closing remembers the place; reopening puts it back", () => {
    const closed = closePane(basic(), "files");
    expect(allPanes(closed.root)).not.toContain("files");
    expect(closed.closed.at(-1)).toEqual({ pane: "files", zoneId: "z2", neighbour: "preview" });
    const { layout, pane } = reopenLastClosed(closed);
    expect(pane).toBe("files");
    expect(zoneOf(layout.root, "files")?.id).toBe("z2");
    expect(layout.closed).toEqual([]);
  });

  it("a pane whose zone is gone comes back beside its old neighbour", () => {
    let layout = closePane(basic(), "files");
    layout = movePane(layout, "preview", { zoneId: "z1", edge: "center" });
    expect(findZone(layout.root, "z2")).toBeNull();
    const { layout: back } = reopenLastClosed(layout);
    expect(zoneOf(back.root, "files")?.id).toBe("z1");
  });

  it("reopen skips panes the caller does not allow", () => {
    let layout = closePane(basic(), "files");
    layout = closePane(layout, "preview");
    const { pane } = reopenLastClosed(layout, (p) => p !== "preview");
    expect(pane).toBe("files");
  });

  it("the closed stack is bounded", () => {
    let layout = basic();
    for (let i = 0; i < MAX_CLOSED + 5; i += 1) {
      layout = insertPane(layout, `p${i}`);
      layout = closePane(layout, `p${i}`);
    }
    expect(layout.closed).toHaveLength(MAX_CLOSED);
    expect(layout.closed.at(-1)?.pane).toBe(`p${MAX_CLOSED + 4}`);
  });

  it("hide keeps the pane in the tree; activate shows it again", () => {
    const hidden = setHidden(basic(), "preview", true);
    expect(allPanes(hidden.root)).toContain("preview");
    expect(hidden.hidden).toEqual(["preview"]);
    expect(activate(hidden, "preview").hidden).toEqual([]);
    expect(setHidden(basic(), CHAT_PANE, true).hidden).toEqual([]);
  });

  it("minimise and restore a zone; activating a tab restores its zone", () => {
    const min = setMinimized(basic(), "z2", true);
    expect(findZone(min.root, "z2")?.minimized).toBe(true);
    expect(findZone(activate(min, "files").root, "z2")?.minimized).toBe(false);
  });

  it("cycles and jumps (9 is the last tab)", () => {
    let layout = insertPane(basic(), "review", { zoneId: "z2", edge: "center" });
    layout = cycleTab(layout, "z2", 1);
    expect(findZone(layout.root, "z2")?.active).toBe("preview");
    layout = cycleTab(layout, "z2", -1);
    expect(findZone(layout.root, "z2")?.active).toBe("review");
    expect(findZone(jumpToSlot(layout, "z2", 2).root, "z2")?.active).toBe("files");
    expect(findZone(jumpToSlot(layout, "z2", 9).root, "z2")?.active).toBe("review");
    expect(jumpToSlot(layout, "z2", 7)).toBe(layout);
  });

  it("resizes with a floor and keeps the split's shape", () => {
    const next = resizeSplit(basic(), "s1", [1, 0]);
    const root = next.root;
    if (root?.kind !== "split") throw new Error("split expected");
    expect(root.sizes[1]).toBeGreaterThan(0);
    expect(resizeSplit(basic(), "s1", [1])).toEqual(basic());
  });

  it("open brings a closed pane back, or activates an open one", () => {
    const layout = closePane(basic(), "files");
    expect(zoneOf(openPane(layout, "files").root, "files")?.id).toBe("z2");
    expect(findZone(openPane(basic(), "files").root, "z2")?.active).toBe("files");
    expect(allPanes(openPane(emptyLayout(), "chat").root)).toEqual(["chat"]);
  });
});

describe("random operations (property)", () => {
  it("never lose or double a pane and always stay canonical", () => {
    for (let seed = 1; seed <= 20; seed += 1) {
      const random = seeded(seed);
      const pick = <T>(items: readonly T[]): T => items[Math.floor(random() * items.length)] as T;
      let layout = initialLayout("advanced", ALL);
      const edges: DropEdge[] = ["center", "left", "right", "top", "bottom"];
      for (let step = 0; step < 500; step += 1) {
        const open = allPanes(layout.root);
        const zoneIds = zones(layout.root).map((z) => z.id);
        const roll = random();
        if (roll < 0.45) {
          layout = movePane(layout, pick(open), {
            zoneId: pick(zoneIds),
            edge: pick(edges),
            index: Math.floor(random() * 4),
          });
        } else if (roll < 0.6) {
          layout = closePane(layout, pick(open));
        } else if (roll < 0.75) {
          layout = reopenLastClosed(layout).layout;
        } else if (roll < 0.85) {
          layout = setMinimized(layout, pick(zoneIds), random() < 0.5);
        } else if (roll < 0.95) {
          layout = setHidden(layout, pick(open), random() < 0.5);
        } else {
          layout = activate(layout, pick(open));
        }
        expect(layoutProblems(layout)).toEqual([]);
        const accounted = [...allPanes(layout.root), ...layout.closed.map((c) => c.pane)];
        expect(new Set(accounted)).toEqual(new Set(ALL));
        expect(accounted).toHaveLength(ALL.length);
        expect(allPanes(layout.root)).toContain(CHAT_PANE);
      }
    }
  });
});

describe("persistence", () => {
  it("round-trips through JSON", () => {
    const layout = setHidden(closePane(basic(), "files"), "preview", true);
    expect(parseLayout(JSON.parse(JSON.stringify(layout)))).toEqual(layout);
  });

  it("repairs duplicates, bad nodes and stale records", () => {
    const repaired = parseLayout({
      version: 1,
      root: {
        kind: "split",
        id: "s1",
        orientation: "row",
        sizes: [1, "x"],
        children: [
          { kind: "zone", id: "z1", tabs: ["chat", "preview"], active: "chat" },
          { kind: "zone", id: "z1", tabs: ["preview", 3], active: "preview" },
          { kind: "weird", id: "q" },
        ],
      },
      hidden: ["preview", "gone"],
      closed: [{ pane: "chat" }, { pane: "files", zoneId: 5 }, { pane: "files" }],
    });
    expect(repaired).not.toBeNull();
    if (repaired === null) return;
    expect(layoutProblems(repaired)).toEqual([]);
    expect(allPanes(repaired.root)).toEqual(["chat", "preview"]);
    expect(repaired.hidden).toEqual(["preview"]);
    expect(repaired.closed).toEqual([{ pane: "files", zoneId: null, neighbour: null }]);
  });

  it("refuses what is not a layout", () => {
    expect(parseLayout(null)).toBeNull();
    expect(parseLayout({ version: 2, root: null })).toBeNull();
    expect(parseLayout("x")).toBeNull();
  });
});

describe("presets", () => {
  it.each(PRESET_IDS)("%s is canonical and always has the chat", (id) => {
    const layout = layoutOf(presetTree(id, ALL));
    expect(layoutProblems(layout)).toEqual([]);
    expect(allPanes(layout.root)).toContain(CHAT_PANE);
  });

  it("focus holds the chat alone; quad uses four zones", () => {
    expect(allPanes(presetTree("focus", ALL))).toEqual(["chat"]);
    expect(zones(presetTree("quad", ALL))).toHaveLength(4);
    expect(zones(presetTree("terminal-deck", ALL)).map((z) => z.tabs)).toEqual([
      ["chat"],
      ["preview", "files", "review"],
      ["terminal"],
    ]);
  });

  it("only places panes that exist", () => {
    expect(allPanes(presetTree("default", ["chat", "files"]))).toEqual(["chat", "files"]);
    expect(presetTree("quad", ["chat"]).kind).toBe("zone");
  });

  it("a preset remembers the panes it leaves out as closed", () => {
    const start = initialLayout("advanced", ALL);
    const focus = applyPreset("focus", start, ALL);
    expect(allPanes(focus.root)).toEqual(["chat"]);
    expect(new Set(focus.closed.map((c) => c.pane))).toEqual(
      new Set(["preview", "files", "review", "terminal"]),
    );
    const back = applyPreset("default", focus, ALL);
    expect(new Set(allPanes(back.root))).toEqual(new Set(ALL));
    expect(back.closed).toEqual([]);
  });
});
