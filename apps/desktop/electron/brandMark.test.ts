import { MARK_PATH } from "@indus/ui";
import { describe, expect, it } from "vitest";
import { coverage, MARK_BOX, markCoverage, pathPolygons } from "./brandMark.ts";

/** Area of one polygon (shoelace). */
function area(poly: readonly (readonly [number, number])[]): number {
  let sum = 0;
  for (let i = 0; i < poly.length; i += 1) {
    const [x0, y0] = poly[i] as readonly [number, number];
    const [x1, y1] = poly[(i + 1) % poly.length] as readonly [number, number];
    sum += x0 * y1 - x1 * y0;
  }
  return sum / 2;
}

describe("brand mark raster", () => {
  it("reads the mark as its three shapes, inside the 100-unit box", () => {
    const polygons = pathPolygons(MARK_PATH);
    expect(polygons).toHaveLength(3);
    for (const poly of polygons) {
      for (const [x, y] of poly) {
        expect(x).toBeGreaterThanOrEqual(0);
        expect(x).toBeLessThanOrEqual(MARK_BOX);
        expect(y).toBeGreaterThanOrEqual(0);
        expect(y).toBeLessThanOrEqual(MARK_BOX);
      }
    }
  });

  it("covers exactly the shapes' area (non-zero fill, exact across each row)", () => {
    const polygons = pathPolygons(MARK_PATH);
    const expected = polygons.reduce((sum, poly) => sum + Math.abs(area(poly)), 0);
    const cover = markCoverage(MARK_BOX, 1, 32);
    const painted = cover.reduce((sum, v) => sum + v, 0);
    expect(Math.abs(painted - expected) / expected).toBeLessThan(0.005);
    // solid inside the bar, empty in the open corner
    expect(cover[50 * MARK_BOX + 50]).toBe(1);
    expect(cover[5 * MARK_BOX + 40]).toBe(0);
  });

  it("handles relative commands, H and V, and partial pixels", () => {
    const square = pathPolygons("m1.5 1.5h2v2H1.5z");
    expect(square).toEqual([
      [
        [1.5, 1.5],
        [3.5, 1.5],
        [3.5, 3.5],
        [1.5, 3.5],
      ],
    ]);
    const cover = coverage(square, 5, 5, { scale: 1, dx: 0, dy: 0 }, 4);
    expect(cover[1 * 5 + 1]).toBeCloseTo(0.25);
    expect(cover[2 * 5 + 2]).toBeCloseTo(1);
    expect(cover[2 * 5 + 1]).toBeCloseTo(0.5);
    expect(() => pathPolygons("M0 0A1 1 0 0 1 2 2")).toThrow(/unsupported/);
  });
});
