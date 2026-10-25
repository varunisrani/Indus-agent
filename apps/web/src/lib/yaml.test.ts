import { describe, expect, it } from "vitest";
import { needsQuotes, nestEntries, toYaml } from "./yaml.ts";

describe("yaml writer", () => {
  it("writes nested maps with two spaces per level", () => {
    expect(toYaml({ model: { name: "fake-model", context: 4096 }, display: { bell: false } })).toBe(
      "model:\n  name: fake-model\n  context: 4096\ndisplay:\n  bell: false\n",
    );
  });

  it("writes lists of scalars and of maps", () => {
    expect(toYaml({ tools: ["web", "files"], empty: [], none: {} })).toBe(
      "tools:\n  - web\n  - files\nempty: []\nnone: {}\n",
    );
    expect(toYaml({ fallback: [{ provider: "a", model: "m" }, { provider: "b" }] })).toBe(
      "fallback:\n  - provider: a\n    model: m\n  - provider: b\n",
    );
  });

  it("quotes strings a reader would misread", () => {
    for (const text of [
      "",
      "true",
      "No",
      "null",
      "~",
      "12",
      "1.5",
      "0x1f",
      " pad",
      "a: b",
      "#x",
      "- y",
      "k:",
      "2026-10-25",
      "1:30",
      "<<",
    ]) {
      expect(needsQuotes(text)).toBe(true);
    }
    for (const text of ["fake-model", "http://127.0.0.1:9119/v1", "a b", "$HOME/x", "v2-1:3"]) {
      expect(needsQuotes(text)).toBe(false);
    }
    expect(toYaml({ v: "yes", none: null, s: "two\nlines", n: 1 })).toBe(
      'v: "yes"\nnone: null\ns: "two\\nlines"\n"n": 1\n',
    );
  });

  it("nests dotted entries and gives nothing for an empty tree", () => {
    const tree = nestEntries([
      { key: "model.provider", value: "fake" },
      { key: "model.name", value: "m" },
      { key: "agent.max_turns", value: 20 },
    ]);
    expect(tree).toEqual({ model: { provider: "fake", name: "m" }, agent: { max_turns: 20 } });
    expect(toYaml({})).toBe("");
  });
});
