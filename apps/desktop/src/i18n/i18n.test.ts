import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { en } from "./en.ts";
import { format, messageKeys } from "./index.tsx";

const SRC = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

function sources(dir: string): string[] {
  const out: string[] = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) out.push(...sources(full));
    else if (/\.(ts|tsx)$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name)) out.push(full);
  }
  return out;
}

const files = sources(SRC).filter((file) => !file.startsWith(path.join(SRC, "i18n")));
const code = files.map((file) => fs.readFileSync(file, "utf8")).join("\n");

describe("i18n", () => {
  it("every key used in a t() call exists in English", () => {
    const used = [...code.matchAll(/\b(?:t|translate)\(\s*"([^"]+)"/g)].map((m) => m[1]);
    const missing = used.filter((key) => key !== undefined && !(key in en));
    expect(missing).toEqual([]);
    expect(used.length).toBeGreaterThan(20);
  });

  it("every English key is used somewhere (key-parity with the code)", () => {
    const unused = messageKeys.filter((key) => !code.includes(`"${key}"`));
    expect(unused).toEqual([]);
  });

  it("no English string is empty and placeholders are filled", () => {
    expect(Object.values(en).filter((text) => text.trim() === "")).toEqual([]);
    expect(format("Go to {text} ({n})", { text: "x", n: 2 })).toBe("Go to x (2)");
    expect(format("keep {missing}", {})).toBe("keep {missing}");
  });

  it("no string is a literal loading text (the Loader is an animated element)", () => {
    expect(Object.values(en).filter((text) => /^loading\b/i.test(text))).toEqual([]);
  });
});
