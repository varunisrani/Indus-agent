import { describe, expect, it } from "vitest";
import {
  graphFromSkills,
  type LearningGraph,
  type LearningNode,
  layout,
  readShareCode,
  SHARE_PREFIX,
  ShareCodeError,
  shareCode,
  timeRange,
  visible,
} from "./layout.ts";

function node(id: string, extra: Partial<LearningNode> = {}): LearningNode {
  return {
    id,
    kind: "skill",
    label: `Label of ${id}`,
    category: "research",
    useCount: 0,
    createdBy: null,
    timestamp: null,
    enabled: true,
    ...extra,
  };
}

const GRAPH: LearningGraph = {
  nodes: [
    node("s1", { label: "pdf-summariser", useCount: 9, timestamp: 100 }),
    node("s2", { label: "git-bisect-helper", createdBy: "agent", timestamp: 200 }),
    node("m1", {
      kind: "memory",
      label: "The user's dog is called Pixel",
      createdBy: "agent",
      timestamp: 300,
      useCount: 2,
    }),
    node("m2", { kind: "memory", label: "Prefers tabs over spaces", timestamp: 400 }),
  ],
  edges: [
    { from: "m1", to: "s1" },
    { from: "s1", to: "s2" },
    { from: "m2", to: "ghost" },
  ],
};
const BOX = { width: 900, height: 560 };

describe("learning map layout", () => {
  it("is deterministic and stays inside the box", () => {
    const one = layout(GRAPH, BOX);
    const two = layout(GRAPH, BOX);
    expect([...one.entries()]).toEqual([...two.entries()]);
    for (const at of one.values()) {
      expect(at.x).toBeGreaterThanOrEqual(10);
      expect(at.x).toBeLessThanOrEqual(BOX.width - 10);
      expect(at.y).toBeGreaterThanOrEqual(10);
      expect(at.y).toBeLessThanOrEqual(BOX.height - 10);
    }
    expect((one.get("s1")?.r ?? 0) > (one.get("s2")?.r ?? 0)).toBe(true); // used more, drawn bigger
  });

  it("the layout share string contains no node text", () => {
    const placed = layout(GRAPH, BOX);
    const code = shareCode(GRAPH, placed, BOX);
    expect(code.startsWith(SHARE_PREFIX)).toBe(true);
    const decoded = atob(code.slice(SHARE_PREFIX.length).replace(/-/g, "+").replace(/_/g, "/"));
    for (const n of GRAPH.nodes) {
      for (const text of [n.id, n.label, n.category, "Pixel", "tabs"]) {
        expect(decoded).not.toContain(text); // short ids may match base64 by chance, so the text
        if (text.length >= 6) expect(code).not.toContain(text); // check is on the decoded body
      }
    }
    expect(Object.keys(JSON.parse(decoded)).sort()).toEqual(["e", "k", "p", "v"]);
  });

  it("a share code round-trips the shape: kinds, positions and edges", () => {
    const placed = layout(GRAPH, BOX);
    const shared = readShareCode(shareCode(GRAPH, placed, BOX));
    expect(shared.nodes.map((n) => n.kind)).toEqual(["skill", "skill", "memory", "memory"]);
    expect(shared.edges).toEqual([
      [2, 0],
      [0, 1],
    ]); // the dangling edge is not shared
    const s1 = placed.get("s1");
    expect(shared.nodes[0]?.x).toBeCloseTo((s1?.x ?? 0) / BOX.width, 3);
    for (const bad of ["", "xyz", `${SHARE_PREFIX}!!!`, `${SHARE_PREFIX}${btoa('{"v":9}')}`]) {
      expect(() => readShareCode(bad)).toThrow(ShareCodeError);
    }
  });

  it("filters and playback pick the right nodes", () => {
    expect(visible(GRAPH, "used", null).nodes.map((n) => n.id)).toEqual(["s1", "m1"]);
    expect(visible(GRAPH, "learned", null).nodes.map((n) => n.id)).toEqual(["s2", "m1"]);
    const early = visible(GRAPH, "all", 250);
    expect(early.nodes.map((n) => n.id)).toEqual(["s1", "s2"]);
    expect(early.edges).toEqual([{ from: "s1", to: "s2" }]);
    expect(timeRange(GRAPH)).toEqual({ from: 100, to: 400 });
    expect(timeRange({ nodes: [node("x")], edges: [] })).toBeNull();
  });

  it("a skills-only graph links each category", () => {
    const graph = graphFromSkills([
      { name: "a", category: "web", enabled: true },
      { name: "b", category: "web", enabled: false },
      { name: "c", category: "", enabled: true },
    ]);
    expect(graph.nodes.map((n) => n.id)).toEqual(["skill:a", "skill:b", "skill:c"]);
    expect(graph.edges).toEqual([{ from: "skill:a", to: "skill:b" }]);
  });
});
