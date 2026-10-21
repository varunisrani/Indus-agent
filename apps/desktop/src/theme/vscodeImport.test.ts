import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { deflateRawSync } from "node:zlib";
import { contrastRatio, semanticNames } from "@indus/ui";
import { describe, expect, it } from "vitest";
import {
  convertVscodeTheme,
  failingPairs,
  mix,
  parseColour,
  parseJsonc,
  readVsix,
  themesFromVsix,
  type VscodeTheme,
} from "./vscodeImport.ts";

const FIXTURE = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../../../../tests/fixtures/desktop/vscode_theme.json",
);
const raw = () => JSON.parse(fs.readFileSync(FIXTURE, "utf8")) as VscodeTheme;

describe("VS Code theme conversion", () => {
  it("converts the fixture deterministically", () => {
    const a = convertVscodeTheme(raw());
    const b = convertVscodeTheme(raw());
    expect(b).toEqual(a);
    expect(a.id).toBe("vscode-riverbank-night");
    expect(a.label).toBe("Riverbank Night");
    expect(a.mode).toBe("dark");
    const hex = Object.fromEntries(semanticNames.map((n) => [n, a.theme[n].hex]));
    expect(hex).toMatchObject({
      bg: "#16202a",
      "bg-raised": "#1b2733",
      fg: "#c9d4dc",
      "accent-fg": "#ffffff",
      danger: "#e0786a",
      warning: "#d6b25e",
      success: "#6cbf8a",
      "focus-ring": "#5aa9d6",
      selection: "#3b8ec255",
    });
    // the code background is translucent in the file: composited over the editor background
    expect(hex["code-bg"]).toBe(mix("#16202a", "#1f2c38", 0x80 / 255));
    expect(a.derived).toEqual([]);
    expect(a.terminal?.brightWhite).toBe("#eef3f6");
    expect(Object.keys(a.terminal ?? {})).toHaveLength(16);
  });

  it("every converted theme passes the contrast pairs", () => {
    expect(failingPairs(convertVscodeTheme(raw()).theme)).toEqual([]);
    const harsh: VscodeTheme = {
      name: "Low Contrast",
      type: "light",
      colors: {
        "editor.background": "#ffffff",
        "editor.foreground": "#dddddd",
        "button.background": "#ffee99",
        descriptionForeground: "#eeeeee",
      },
    };
    const converted = convertVscodeTheme(harsh);
    expect(failingPairs(converted.theme)).toEqual([]);
    expect(contrastRatio(converted.theme.fg.hex, "#ffffff")).toBeGreaterThanOrEqual(4.5);
  });

  it("derives what is missing and guesses the mode from the background", () => {
    const bare = convertVscodeTheme({ colors: { "editor.background": "#f4f1ea" } }, "Paper");
    expect(bare.mode).toBe("light");
    expect(bare.label).toBe("Paper");
    expect(bare.derived).toContain("accent");
    expect(bare.terminal).toBeNull();
    expect(convertVscodeTheme({}).mode).toBe("dark");
  });

  it("reads JSONC with comments and trailing commas, and short colours", () => {
    const text = `{
      // a line comment
      "name": "With // slashes in a string",
      /* a block comment */
      "colors": { "editor.background": "#123", },
    }`;
    const parsed = parseJsonc(text) as VscodeTheme;
    expect(parsed.name).toBe("With // slashes in a string");
    expect(convertVscodeTheme(parsed).theme.bg.hex).toBe("#112233");
    expect(parseColour("#abcd")).toEqual({ hex: "#aabbcc", alpha: 0xdd / 255 });
    expect(parseColour("red")).toBeNull();
  });
});

// -- a VSIX built here --------------------------------------------------------------------------------

function zip(files: Record<string, string>): Uint8Array {
  const chunks: Buffer[] = [];
  const central: Buffer[] = [];
  let offset = 0;
  for (const [name, text] of Object.entries(files)) {
    const data = deflateRawSync(Buffer.from(text));
    const nameBytes = Buffer.from(name);
    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50, 0);
    local.writeUInt16LE(8, 8);
    local.writeUInt32LE(data.length, 18);
    local.writeUInt32LE(Buffer.byteLength(text), 22);
    local.writeUInt16LE(nameBytes.length, 26);
    chunks.push(local, nameBytes, data);
    const entry = Buffer.alloc(46);
    entry.writeUInt32LE(0x02014b50, 0);
    entry.writeUInt16LE(8, 10);
    entry.writeUInt32LE(data.length, 20);
    entry.writeUInt32LE(Buffer.byteLength(text), 24);
    entry.writeUInt16LE(nameBytes.length, 28);
    entry.writeUInt32LE(offset, 42);
    central.push(entry, nameBytes);
    offset += local.length + nameBytes.length + data.length;
  }
  const directory = Buffer.concat(central);
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0);
  end.writeUInt16LE(Object.keys(files).length, 8);
  end.writeUInt16LE(Object.keys(files).length, 10);
  end.writeUInt32LE(directory.length, 12);
  end.writeUInt32LE(offset, 16);
  return new Uint8Array(Buffer.concat([...chunks, directory, end]));
}

describe("extension packages", () => {
  it("reads the theme the manifest lists and converts it", async () => {
    const bytes = zip({
      "extension/package.json": JSON.stringify({
        contributes: {
          themes: [{ label: "Riverbank", uiTheme: "vs-dark", path: "./themes/river.json" }],
        },
      }),
      "extension/themes/river.json": fs.readFileSync(FIXTURE, "utf8"),
    });
    expect(readVsix(bytes).names()).toContain("extension/themes/river.json");
    const themes = await themesFromVsix(bytes);
    expect(themes).toHaveLength(1);
    expect(themes[0]?.label).toBe("Riverbank");
    expect(themes[0]?.theme.bg.hex).toBe("#16202a");
  });

  it("refuses what is not a theme package", async () => {
    expect(() => readVsix(new Uint8Array([1, 2, 3]))).toThrow(/not a zip/);
    const noThemes = zip({ "extension/package.json": "{}" });
    await expect(themesFromVsix(noThemes)).rejects.toThrow(/no colour theme/);
  });
});
