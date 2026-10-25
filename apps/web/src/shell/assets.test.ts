import { readdirSync, readFileSync } from "node:fs";
import { join, relative } from "node:path";
import { describe, expect, it } from "vitest";

// R09.22 / ACC-L20-1, amended by the owner's UI-parity decision (W51-a-2): the app's own tree holds
// no font files and no imagery, so no vendor brand asset can ride along. Type comes from open-licence
// @fontsource packages imported by src/theme/fonts.ts; stylesheets in src never load fonts directly.
const APP_ROOT = join(import.meta.dirname, "..", "..");
const MEDIA = /\.(woff2?|ttf|otf|eot|png|jpe?g|gif|webp|avif|ico|svg)$/i;

function walk(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    if (entry.name === "node_modules" || entry.name === "dist") return [];
    const path = join(dir, entry.name);
    return entry.isDirectory() ? walk(path) : [path];
  });
}

describe("dashboard assets", () => {
  it("ships no font files or brand imagery", () => {
    const media = walk(APP_ROOT)
      .filter((path) => MEDIA.test(path))
      .map((path) => relative(APP_ROOT, path));
    expect(media).toEqual([]);
  });

  it("loads no web fonts from stylesheets", () => {
    const css = walk(join(APP_ROOT, "src"))
      .filter((path) => path.endsWith(".css"))
      .map((path) => readFileSync(path, "utf8"))
      .join("\n");
    expect(css).not.toMatch(/@font-face|fonts\.googleapis|url\(/i);
  });
});
