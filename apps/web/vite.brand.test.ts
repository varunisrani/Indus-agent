import { readFileSync } from "node:fs";
import { join } from "node:path";
import { BRAND_COLORS, BRAND_NAME, MARK_PATH } from "@indus/ui";
import { describe, expect, it } from "vitest";
import { brandFavicon, FAVICON_PATH, faviconSvg } from "./vite.brand.ts";

const INDEX_HTML = readFileSync(join(import.meta.dirname, "index.html"), "utf8");

describe("brand favicon", () => {
  it("is the white mark on an ember square", () => {
    const svg = faviconSvg();
    expect(svg.startsWith('<svg xmlns="http://www.w3.org/2000/svg"')).toBe(true);
    expect(svg).toContain(`<rect width="160" height="160" fill="${BRAND_COLORS.ember}"/>`);
    expect(svg).toContain(`fill="${BRAND_COLORS.white}" d="${MARK_PATH}"`);
  });

  it("is emitted into the bundle root", () => {
    const emitted: { fileName?: string; source?: unknown }[] = [];
    const plugin = brandFavicon();
    const hook = plugin.generateBundle as unknown as (this: unknown) => void;
    hook.call({ emitFile: (file: { fileName?: string; source?: unknown }) => emitted.push(file) });
    expect(emitted).toEqual([{ type: "asset", fileName: FAVICON_PATH, source: faviconSvg() }]);
  });

  it("is linked from index.html, which carries the brand name", () => {
    expect(INDEX_HTML).toContain(
      `<link rel="icon" type="image/svg+xml" href="/${FAVICON_PATH}" />`,
    );
    expect(INDEX_HTML).toContain(`<title>${BRAND_NAME}</title>`);
    expect(INDEX_HTML).toContain(`The ${BRAND_NAME} dashboard needs JavaScript.`);
  });
});
