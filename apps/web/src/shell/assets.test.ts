import { readdirSync, readFileSync } from "node:fs";
import { join, relative } from "node:path";
import { describe, expect, it } from "vitest";

// R09.22 / ACC-L20-1: the dashboard ships no font files and no imagery of its own yet; type comes
// from system stacks named in @indus/ui, so no vendor brand asset can ride along.
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
