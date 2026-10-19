import { EventEmitter } from "node:events";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  applyTruecolorOptIn,
  clearProbeCache,
  detectFromEnv,
  OSC11_QUERY,
  parseOscColour,
  probeBackground,
  resolveLightMode,
  trustedProbeColour,
} from "./detect.ts";

afterEach(() => {
  clearProbeCache();
  vi.useRealTimers();
});

describe("light/dark detection order", () => {
  const rows: [string, Record<string, string>, boolean | null, string | null][] = [
    [
      "INDUS_TUI_LIGHT yes beats everything",
      { INDUS_TUI_LIGHT: "yes", INDUS_TUI_THEME: "dark", COLORFGBG: "0;0" },
      true,
      "INDUS_TUI_LIGHT",
    ],
    [
      "INDUS_TUI_LIGHT off beats a light theme name",
      { INDUS_TUI_LIGHT: "off", INDUS_TUI_THEME: "light" },
      false,
      "INDUS_TUI_LIGHT",
    ],
    [
      "INDUS_TUI_THEME light",
      { INDUS_TUI_THEME: "light", COLORFGBG: "15;0" },
      true,
      "INDUS_TUI_THEME",
    ],
    [
      "INDUS_TUI_THEME dark",
      { INDUS_TUI_THEME: "Dark", COLORFGBG: "0;15" },
      false,
      "INDUS_TUI_THEME",
    ],
    ["INDUS_TUI_THEME hex background", { INDUS_TUI_THEME: "#fdf6e3" }, true, "INDUS_TUI_THEME"],
    [
      "INDUS_TUI_BACKGROUND hint, dark",
      { INDUS_TUI_BACKGROUND: "1e1e2e", COLORFGBG: "0;15" },
      false,
      "INDUS_TUI_BACKGROUND",
    ],
    [
      "INDUS_TUI_BACKGROUND hint, light 3-digit",
      { INDUS_TUI_BACKGROUND: "#eee" },
      true,
      "INDUS_TUI_BACKGROUND",
    ],
    ["COLORFGBG slot 15 is light", { COLORFGBG: "0;15" }, true, "COLORFGBG"],
    ["COLORFGBG slot 7 is light", { COLORFGBG: "0;default;7" }, true, "COLORFGBG"],
    [
      "COLORFGBG dark slot is authoritative over TERM_PROGRAM",
      { COLORFGBG: "15;0", TERM_PROGRAM: "Apple_Terminal" },
      false,
      "COLORFGBG",
    ],
    [
      "malformed COLORFGBG falls through to TERM_PROGRAM",
      { COLORFGBG: "15;", TERM_PROGRAM: "Apple_Terminal" },
      true,
      "TERM_PROGRAM",
    ],
    ["TERM_PROGRAM light-default list", { TERM_PROGRAM: "Apple_Terminal" }, true, "TERM_PROGRAM"],
    ["junk hex is ignored", { INDUS_TUI_BACKGROUND: "fffgff" }, null, null],
    ["nothing decides", { TERM_PROGRAM: "WezTerm" }, null, null],
  ];
  for (const [label, env, light, source] of rows) {
    it(label, () => {
      const decision = detectFromEnv(env);
      expect(decision?.light ?? null).toBe(light);
      expect(decision?.source ?? null).toBe(source);
    });
  }

  it("asks the terminal only when the environment does not decide", async () => {
    const probe = vi.fn(async () => "#fafafa");
    expect((await resolveLightMode({ COLORFGBG: "0;0" }, probe)).light).toBe(false);
    expect(probe).not.toHaveBeenCalled();
    const fromProbe = await resolveLightMode({}, probe);
    expect(fromProbe).toEqual({ light: true, source: "osc11", background: "#fafafa" });
  });

  it("falls back to dark when the probe has no trusted answer", async () => {
    const decision = await resolveLightMode({}, async () => null);
    expect(decision).toEqual({ light: false, source: "default", background: null });
  });
});

describe("OSC 11 answers", () => {
  it("parses the reply forms terminals send", () => {
    expect(parseOscColour("\u001b]11;rgb:ffff/fafa/f0f0\u0007")).toBe("#fffaf0");
    expect(parseOscColour("\u001b]11;rgb:1e/1e/2e\u001b\\")).toBe("#1e1e2e");
    expect(parseOscColour("rgba:0000/0000/0000/ffff")).toBe("#000000");
    expect(parseOscColour("#123")).toBe("#112233");
    expect(parseOscColour("#1111aaaa2222")).toBe("#11aa22");
    expect(parseOscColour("rgb:zz/00/00")).toBeNull();
  });

  it("treats pure black and pure white as unset", () => {
    expect(trustedProbeColour("#000000")).toBeNull();
    expect(trustedProbeColour("#ffffff")).toBeNull();
    expect(trustedProbeColour("#101010")).toBe("#101010");
  });

  it("probes once, reads a reply split across chunks, and caches it", async () => {
    const input = new EventEmitter();
    const writes: string[] = [];
    const output = { write: (t: string) => writes.push(t) };
    const first = probeBackground({ input, output }, 1000);
    input.emit("data", "\u001b]11;rgb:2020/");
    input.emit("data", "2424/2828\u0007");
    expect(await first).toBe("#202428");
    expect(writes).toEqual([OSC11_QUERY]);
    expect(await probeBackground({ input, output }, 1000)).toBe("#202428");
    expect(writes).toHaveLength(1);
  });

  it("ignores a #000000 answer and gives up after the timeout", async () => {
    vi.useFakeTimers();
    const input = new EventEmitter();
    const black = probeBackground({ input, output: { write: () => true } }, 100);
    input.emit("data", "\u001b]11;rgb:0000/0000/0000\u0007");
    expect(await black).toBeNull();
    clearProbeCache();
    const silent = probeBackground({ input, output: { write: () => true } }, 100);
    await vi.advanceTimersByTimeAsync(100);
    expect(await silent).toBeNull();
    expect(input.listenerCount("data")).toBe(0);
  });
});

describe("truecolor opt-in", () => {
  it("forces truecolor only when asked", () => {
    const env: Record<string, string | undefined> = {};
    expect(applyTruecolorOptIn(env)).toBe(false);
    expect(env.FORCE_COLOR).toBeUndefined();
    env.INDUS_TUI_TRUECOLOR = "1";
    expect(applyTruecolorOptIn(env)).toBe(true);
    expect(env.FORCE_COLOR).toBe("3");
  });
});
