// The learning map's geometry (P24-28): a deterministic force-directed layout of skills and
// memories, the filters (all / used / learned) and time playback, and the share code.
//
// Layout: every node starts on a circle at an angle taken from a hash of its id, so the same graph
// always draws the same picture; then a fixed number of steps applies pairwise repulsion, a spring
// along each edge and a pull toward the centre, with a step size that cools linearly. Older nodes
// (by timestamp rank) are pulled toward a wider ring, so the newest learning sits near the middle.
//
// Share code: `ilm1.` + base64url(JSON {v, k, p, e}) — node kinds (0 skill, 1 memory), positions
// quantised to 0..4095 in the drawing box, and edges as index pairs. It never carries a node's id,
// label, category, use count or any memory text: sharing shows the shape of what was learned,
// never what it says.

export type NodeKind = "skill" | "memory";
export type MapFilter = "all" | "used" | "learned";

export interface LearningNode {
  readonly id: string;
  readonly kind: NodeKind;
  readonly label: string;
  readonly category: string;
  readonly useCount: number;
  /** Who made it: the agent learning on its own, the person, or unknown. */
  readonly createdBy: "agent" | "user" | null;
  /** Unix seconds, null when unknown. */
  readonly timestamp: number | null;
  readonly enabled: boolean;
}

export interface LearningEdge {
  readonly from: string;
  readonly to: string;
}

export interface LearningGraph {
  readonly nodes: readonly LearningNode[];
  readonly edges: readonly LearningEdge[];
}

export interface Placed {
  readonly x: number;
  readonly y: number;
  readonly r: number;
}

export interface LayoutOptions {
  width: number;
  height: number;
  iterations?: number;
}

export const SHARE_PREFIX = "ilm1.";
const GRID = 4095;
const MAX_LAYOUT_NODES = 600;

function hash(text: string): number {
  let h = 2166136261;
  for (let i = 0; i < text.length; i += 1) {
    h ^= text.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

export function radiusOf(node: Pick<LearningNode, "useCount" | "kind">): number {
  const base = node.kind === "skill" ? 6 : 4;
  return base + Math.min(10, Math.log2(1 + Math.max(0, node.useCount)) * 2);
}

/** Positions for every node of `graph` in a `width` × `height` box (deterministic). */
export function layout(graph: LearningGraph, options: LayoutOptions): Map<string, Placed> {
  const nodes = graph.nodes.slice(0, MAX_LAYOUT_NODES);
  const n = nodes.length;
  const cx = options.width / 2;
  const cy = options.height / 2;
  const span = Math.min(options.width, options.height) / 2 - 20;
  const index = new Map(nodes.map((node, i) => [node.id, i]));
  const order = nodes
    .map((node, i) => ({ i, ts: node.timestamp ?? Number.POSITIVE_INFINITY }))
    .sort((a, b) => a.ts - b.ts);
  const age = new Array<number>(n).fill(0);
  order.forEach((entry, rank) => {
    age[entry.i] = n <= 1 ? 0 : 1 - rank / (n - 1); // 1 = oldest, 0 = newest
  });
  const xs = new Float64Array(n);
  const ys = new Float64Array(n);
  nodes.forEach((node, i) => {
    const angle = ((hash(node.id) % 3600) / 3600) * Math.PI * 2;
    const ring = span * (0.25 + 0.6 * (age[i] ?? 0));
    xs[i] = cx + Math.cos(angle) * ring;
    ys[i] = cy + Math.sin(angle) * ring;
  });
  const edges = graph.edges
    .map((e) => [index.get(e.from), index.get(e.to)] as const)
    .filter(
      (pair): pair is readonly [number, number] => pair[0] !== undefined && pair[1] !== undefined,
    );
  const steps = options.iterations ?? 200;
  const ideal = Math.max(18, span / Math.sqrt(Math.max(1, n)));
  for (let step = 0; step < steps; step += 1) {
    const cool = 1 - step / steps;
    const dx = new Float64Array(n);
    const dy = new Float64Array(n);
    for (let a = 0; a < n; a += 1) {
      for (let b = a + 1; b < n; b += 1) {
        let ex = (xs[a] as number) - (xs[b] as number);
        let ey = (ys[a] as number) - (ys[b] as number);
        let d2 = ex * ex + ey * ey;
        if (d2 < 0.01) {
          ex = ((a * 7 + b) % 5) - 2 || 1;
          ey = ((a + b * 3) % 5) - 2 || 1;
          d2 = ex * ex + ey * ey;
        }
        const force = (ideal * ideal) / d2;
        dx[a] = (dx[a] as number) + ex * force;
        dy[a] = (dy[a] as number) + ey * force;
        dx[b] = (dx[b] as number) - ex * force;
        dy[b] = (dy[b] as number) - ey * force;
      }
    }
    for (const [a, b] of edges) {
      const ex = (xs[a] as number) - (xs[b] as number);
      const ey = (ys[a] as number) - (ys[b] as number);
      const d = Math.sqrt(ex * ex + ey * ey) || 1;
      const pull = (d - ideal) / d / 4;
      dx[a] = (dx[a] as number) - ex * pull;
      dy[a] = (dy[a] as number) - ey * pull;
      dx[b] = (dx[b] as number) + ex * pull;
      dy[b] = (dy[b] as number) + ey * pull;
    }
    for (let i = 0; i < n; i += 1) {
      const ring = span * (0.15 + 0.7 * (age[i] ?? 0));
      const ox = (xs[i] as number) - cx;
      const oy = (ys[i] as number) - cy;
      const dist = Math.sqrt(ox * ox + oy * oy) || 1;
      dx[i] = (dx[i] as number) - (ox / dist) * (dist - ring) * 0.1;
      dy[i] = (dy[i] as number) - (oy / dist) * (dist - ring) * 0.1;
      const len = Math.sqrt((dx[i] as number) ** 2 + (dy[i] as number) ** 2) || 1;
      const limit = Math.max(0.5, ideal * cool);
      xs[i] = Math.min(
        options.width - 10,
        Math.max(10, (xs[i] as number) + ((dx[i] as number) / len) * Math.min(len, limit)),
      );
      ys[i] = Math.min(
        options.height - 10,
        Math.max(10, (ys[i] as number) + ((dy[i] as number) / len) * Math.min(len, limit)),
      );
    }
  }
  const out = new Map<string, Placed>();
  nodes.forEach((node, i) => {
    out.set(node.id, { x: xs[i] as number, y: ys[i] as number, r: radiusOf(node) });
  });
  return out;
}

/** The nodes `filter` keeps that existed by `until` (unix seconds; null = now), with their edges. */
export function visible(
  graph: LearningGraph,
  filter: MapFilter,
  until: number | null,
): LearningGraph {
  const nodes = graph.nodes.filter((node) => {
    if (filter === "used" && node.useCount <= 0) return false;
    if (filter === "learned" && node.createdBy !== "agent") return false;
    if (until !== null && node.timestamp !== null && node.timestamp > until) return false;
    return true;
  });
  const kept = new Set(nodes.map((node) => node.id));
  return { nodes, edges: graph.edges.filter((e) => kept.has(e.from) && kept.has(e.to)) };
}

/** The playback range: earliest and latest known timestamps (null without any). */
export function timeRange(graph: LearningGraph): { from: number; to: number } | null {
  const stamps = graph.nodes.map((n) => n.timestamp).filter((t): t is number => t !== null);
  if (stamps.length === 0) return null;
  return { from: Math.min(...stamps), to: Math.max(...stamps) };
}

function toBase64Url(text: string): string {
  const bytes = new TextEncoder().encode(text);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function fromBase64Url(code: string): string {
  const padded = code
    .replace(/-/g, "+")
    .replace(/_/g, "/")
    .padEnd(Math.ceil(code.length / 4) * 4, "=");
  const binary = atob(padded);
  const bytes = Uint8Array.from(binary, (c) => c.charCodeAt(0));
  return new TextDecoder().decode(bytes);
}

/** The share code of a drawn map: kinds, quantised positions and edges only. */
export function shareCode(
  graph: LearningGraph,
  placed: ReadonlyMap<string, Placed>,
  box: { width: number; height: number },
): string {
  const nodes = graph.nodes.filter((node) => placed.has(node.id));
  const index = new Map(nodes.map((node, i) => [node.id, i]));
  const q = (value: number, size: number) =>
    Math.max(0, Math.min(GRID, Math.round((value / Math.max(1, size)) * GRID)));
  const body = {
    v: 1,
    k: nodes.map((node) => (node.kind === "skill" ? 0 : 1)),
    p: nodes.map((node) => {
      const at = placed.get(node.id) as Placed;
      return [q(at.x, box.width), q(at.y, box.height)];
    }),
    e: graph.edges
      .map((edge) => [index.get(edge.from), index.get(edge.to)])
      .filter((pair): pair is [number, number] => pair[0] !== undefined && pair[1] !== undefined),
  };
  return `${SHARE_PREFIX}${toBase64Url(JSON.stringify(body))}`;
}

export interface SharedMap {
  nodes: Array<{ kind: NodeKind; x: number; y: number }>;
  edges: Array<[number, number]>;
}

export class ShareCodeError extends Error {}

/** Read a share code into fractions of the box (0..1); refuses anything else. */
export function readShareCode(code: string): SharedMap {
  const trimmed = code.trim();
  if (!trimmed.startsWith(SHARE_PREFIX))
    throw new ShareCodeError("this is not a learning map code");
  let body: { v?: unknown; k?: unknown; p?: unknown; e?: unknown };
  try {
    body = JSON.parse(fromBase64Url(trimmed.slice(SHARE_PREFIX.length))) as typeof body;
  } catch {
    throw new ShareCodeError("the code is damaged");
  }
  if (body.v !== 1 || !Array.isArray(body.k) || !Array.isArray(body.p) || !Array.isArray(body.e)) {
    throw new ShareCodeError("the code is from another version");
  }
  const kinds = body.k as unknown[];
  const points = body.p as unknown[];
  if (kinds.length !== points.length || kinds.length > 5000)
    throw new ShareCodeError("the code is damaged");
  const nodes = kinds.map((kind, i) => {
    const point = points[i] as unknown;
    if (!Array.isArray(point) || point.length !== 2 || (kind !== 0 && kind !== 1)) {
      throw new ShareCodeError("the code is damaged");
    }
    const [x, y] = point as [unknown, unknown];
    if (typeof x !== "number" || typeof y !== "number")
      throw new ShareCodeError("the code is damaged");
    return { kind: (kind === 0 ? "skill" : "memory") as NodeKind, x: x / GRID, y: y / GRID };
  });
  const edges = (body.e as unknown[]).filter(
    (pair): pair is [number, number] =>
      Array.isArray(pair) &&
      pair.length === 2 &&
      Number.isInteger(pair[0]) &&
      Number.isInteger(pair[1]) &&
      (pair[0] as number) < nodes.length &&
      (pair[1] as number) < nodes.length,
  );
  return { nodes, edges };
}

/** A graph from the skills list alone (no learning-graph read on the server yet). */
export function graphFromSkills(
  skills: ReadonlyArray<{ name: string; category: string; enabled: boolean; source?: string }>,
): LearningGraph {
  const nodes: LearningNode[] = skills.map((skill) => ({
    id: `skill:${skill.name}`,
    kind: "skill",
    label: skill.name,
    category: skill.category,
    useCount: 0,
    createdBy: skill.source === "agent" ? "agent" : null,
    timestamp: null,
    enabled: skill.enabled,
  }));
  const edges: LearningEdge[] = [];
  const byCategory = new Map<string, string[]>();
  for (const node of nodes) {
    if (node.category === "") continue;
    const group = byCategory.get(node.category) ?? [];
    group.push(node.id);
    byCategory.set(node.category, group);
  }
  for (const group of byCategory.values()) {
    for (let i = 1; i < group.length; i += 1)
      edges.push({ from: group[0] as string, to: group[i] as string });
  }
  return { nodes, edges };
}
