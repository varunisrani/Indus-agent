// The brand mark as pixels (W63): `@indus/ui` MARK_PATH flattened to polygons and filled with exact
// horizontal coverage on supersampled rows (non-zero winding), in plain arithmetic so the same
// input gives the same bytes on every machine. The tray glyph (ambient/trayIcon.ts) and the app
// icons (scripts/make-icons.mjs) are both drawn from it; no image library, no browser.

import { MARK_PATH } from "@indus/ui";

export type Point = readonly [number, number];
export type Polygon = readonly Point[];

/** The mark's coordinate box is 100 × 100 (`MARK_VIEWBOX`). */
export const MARK_BOX = 100;

const TOKEN = /([A-Za-z])|(-?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)/g;

/**
 * An SVG path (`M L H V C Z`, absolute or relative) as closed polygons, cubic curves flattened into
 * `segments` straight pieces each.
 */
export function pathPolygons(d: string, segments = 12): Polygon[] {
  const tokens = [...d.matchAll(TOKEN)].map((m) => m[0]);
  const polygons: Point[][] = [];
  let current: Point[] = [];
  let x = 0;
  let y = 0;
  let start: Point = [0, 0];
  let command = "";
  let at = 0;
  const num = (): number => {
    const value = Number(tokens[at]);
    at += 1;
    if (!Number.isFinite(value)) throw new Error(`bad number in path at token ${at}`);
    return value;
  };
  const close = () => {
    if (current.length > 2) polygons.push(current);
    current = [];
  };
  while (at < tokens.length) {
    const token = tokens[at] as string;
    if (/^[A-Za-z]$/.test(token)) {
      command = token;
      at += 1;
      if (command === "Z" || command === "z") {
        close();
        x = start[0];
        y = start[1];
        continue;
      }
    }
    const rel = command === command.toLowerCase();
    const ox = rel ? x : 0;
    const oy = rel ? y : 0;
    switch (command.toUpperCase()) {
      case "M": {
        close();
        x = ox + num();
        y = oy + num();
        start = [x, y];
        current.push(start);
        command = rel ? "l" : "L"; // further pairs are line-tos
        break;
      }
      case "L":
        x = ox + num();
        y = oy + num();
        current.push([x, y]);
        break;
      case "H":
        x = ox + num();
        current.push([x, y]);
        break;
      case "V":
        y = oy + num();
        current.push([x, y]);
        break;
      case "C": {
        const x1 = ox + num();
        const y1 = oy + num();
        const x2 = ox + num();
        const y2 = oy + num();
        const x3 = ox + num();
        const y3 = oy + num();
        for (let i = 1; i <= segments; i += 1) {
          const t = i / segments;
          const u = 1 - t;
          const a = u * u * u;
          const b = 3 * u * u * t;
          const c = 3 * u * t * t;
          const e = t * t * t;
          current.push([a * x + b * x1 + c * x2 + e * x3, a * y + b * y1 + c * y2 + e * y3]);
        }
        x = x3;
        y = y3;
        break;
      }
      default:
        throw new Error(`unsupported path command ${command || token}`);
    }
  }
  close();
  return polygons;
}

/** Where the shape lands: device = shape × scale + offset. */
export interface Placement {
  readonly scale: number;
  readonly dx: number;
  readonly dy: number;
}

/**
 * Coverage (0..1) of each pixel of a `width` × `height` grid by the polygons (non-zero winding):
 * `rows` sample rows per pixel, each with the exact covered length across the row.
 */
export function coverage(
  polygons: readonly Polygon[],
  width: number,
  height: number,
  place: Placement,
  rows = 8,
): Float64Array {
  const out = new Float64Array(width * height);
  const edges: Array<{ x0: number; y0: number; x1: number; y1: number; dir: number }> = [];
  for (const poly of polygons) {
    for (let i = 0; i < poly.length; i += 1) {
      const a = poly[i] as Point;
      const b = poly[(i + 1) % poly.length] as Point;
      const ax = a[0] * place.scale + place.dx;
      const ay = a[1] * place.scale + place.dy;
      const bx = b[0] * place.scale + place.dx;
      const by = b[1] * place.scale + place.dy;
      if (ay === by) continue;
      edges.push(
        ay < by
          ? { x0: ax, y0: ay, x1: bx, y1: by, dir: 1 }
          : { x0: bx, y0: by, x1: ax, y1: ay, dir: -1 },
      );
    }
  }
  const weight = 1 / rows;
  const hits: Array<{ x: number; dir: number }> = [];
  for (let py = 0; py < height; py += 1) {
    for (let r = 0; r < rows; r += 1) {
      const sy = py + (r + 0.5) / rows;
      hits.length = 0;
      for (const e of edges) {
        if (sy < e.y0 || sy >= e.y1) continue;
        hits.push({ x: e.x0 + ((sy - e.y0) / (e.y1 - e.y0)) * (e.x1 - e.x0), dir: e.dir });
      }
      if (hits.length < 2) continue;
      hits.sort((p, q) => p.x - q.x || p.dir - q.dir);
      let winding = 0;
      for (let i = 0; i < hits.length - 1; i += 1) {
        winding += (hits[i] as { dir: number }).dir;
        if (winding === 0) continue;
        const from = Math.max(0, (hits[i] as { x: number }).x);
        const to = Math.min(width, (hits[i + 1] as { x: number }).x);
        if (to <= from) continue;
        const row = py * width;
        const first = Math.floor(from);
        const last = Math.min(width - 1, Math.floor(to));
        if (first === last) {
          out[row + first] = (out[row + first] as number) + (to - from) * weight;
          continue;
        }
        out[row + first] = (out[row + first] as number) + (first + 1 - from) * weight;
        for (let px = first + 1; px < last; px += 1) {
          out[row + px] = (out[row + px] as number) + weight;
        }
        if (last < width) out[row + last] = (out[row + last] as number) + (to - last) * weight;
      }
    }
  }
  for (let i = 0; i < out.length; i += 1) out[i] = Math.min(1, out[i] as number);
  return out;
}

let markPolygons: Polygon[] | null = null;

/** The mark's polygons (parsed once). */
export function mark(): readonly Polygon[] {
  markPolygons ??= pathPolygons(MARK_PATH);
  return markPolygons;
}

/** The mark's coverage on a `size` square, the mark's box filling `fraction` of it, centred. */
export function markCoverage(size: number, fraction = 1, rows = 8): Float64Array {
  const scale = (size * fraction) / MARK_BOX;
  const offset = (size - MARK_BOX * scale) / 2;
  return coverage(mark(), size, size, { scale, dx: offset, dy: offset }, rows);
}
