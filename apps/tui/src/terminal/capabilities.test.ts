import { EventEmitter } from "node:events";
import { describe, expect, it } from "vitest";
import {
  detectCapabilities,
  editorHandoff,
  extendedKeys,
  keyModesOff,
  keyModesOn,
  mouseOff,
  mouseOn,
  mousePreset,
  syncOutputSupported,
} from "./capabilities.ts";
import { seq } from "./modes.ts";
import { gateStdout } from "./outputGate.ts";

describe("synchronized output", () => {
  const rows: [string, Record<string, string>, boolean][] = [
    ["kitty by TERM", { TERM: "xterm-kitty" }, true],
    ["kitty by window id", { KITTY_WINDOW_ID: "1" }, true],
    ["iTerm2", { TERM_PROGRAM: "iTerm.app" }, true],
    ["WezTerm", { TERM_PROGRAM: "WezTerm" }, true],
    ["Ghostty", { TERM: "xterm-ghostty" }, true],
    ["Zed", { ZED_TERM: "true" }, true],
    ["VS Code", { TERM_PROGRAM: "vscode" }, true],
    ["Windows Terminal", { WT_SESSION: "abc" }, true],
    ["unknown xterm", { TERM: "xterm-256color" }, false],
    ["Apple Terminal", { TERM_PROGRAM: "Apple_Terminal" }, false],
    ["tmux inside WezTerm", { TMUX: "/tmp/tmux-0/default,1,0", TERM_PROGRAM: "WezTerm" }, false],
    ["tmux inside kitty", { TMUX: "/tmp/t,2,0", TERM: "xterm-kitty" }, false],
    ["zellij", { ZELLIJ: "0", TERM_PROGRAM: "ghostty" }, false],
    ["screen", { STY: "123.pts", WT_SESSION: "x" }, false],
  ];
  for (const [label, env, expected] of rows) {
    it(`${label} → ${expected ? "wraps frames" : "no 2026 bytes"}`, () => {
      expect(syncOutputSupported(env)).toBe(expected);
    });
  }
});

class FakeOut extends EventEmitter {
  isTTY = true;
  columns = 80;
  rows = 24;
  out = "";
  write(chunk: string, cb?: () => void): boolean {
    this.out += chunk;
    cb?.();
    return true;
  }
}

function frame(stream: NodeJS.WriteStream): void {
  stream.write(seq.syncBegin);
  stream.write("frame body");
  stream.write(seq.syncEnd);
}

describe("output gate", () => {
  it("under tmux drops every 2026 marker Ink writes", () => {
    const out = new FakeOut();
    const caps = detectCapabilities({ TMUX: "x", TERM_PROGRAM: "WezTerm" });
    const { stream, stats } = gateStdout(out as unknown as NodeJS.WriteStream, {
      syncOutput: caps.syncOutput,
    });
    frame(stream);
    stream.write(`${seq.syncBegin}inline${seq.syncEnd}`);
    expect(out.out).toBe("frame bodyinline");
    expect(out.out).not.toContain("\u001b[?2026");
    expect(stats.markersDropped).toBe(3);
  });

  it("in kitty keeps the markers around each frame", () => {
    const out = new FakeOut();
    const caps = detectCapabilities({ TERM: "xterm-kitty" });
    const { stream, stats } = gateStdout(out as unknown as NodeJS.WriteStream, {
      syncOutput: caps.syncOutput,
    });
    frame(stream);
    frame(stream);
    expect(out.out).toBe(`${seq.syncBegin}frame body${seq.syncEnd}`.repeat(2));
    expect(stats.frames).toBe(2);
  });

  it("reports write failures and successes", () => {
    const out = new FakeOut();
    const seen: string[] = [];
    const { stream } = gateStdout(out as unknown as NodeJS.WriteStream, {
      syncOutput: true,
      onWriteOk: () => seen.push("ok"),
      onWriteError: (e) => seen.push((e as { code: string }).code),
    });
    stream.write("a");
    out.write = () => {
      throw Object.assign(new Error("EPIPE"), { code: "EPIPE" });
    };
    expect(stream.write("b")).toBe(false);
    expect(seen).toEqual(["ok", "EPIPE"]);
  });
});

describe("extended keys", () => {
  it("enables both on the known terminals and nothing elsewhere", () => {
    expect(extendedKeys({ TERM: "xterm-kitty" })).toEqual({ kitty: true, otherKeys: true });
    expect(extendedKeys({ TERM_PROGRAM: "iTerm.app" })).toEqual({ kitty: true, otherKeys: true });
    expect(extendedKeys({ WT_SESSION: "1" })).toEqual({ kitty: true, otherKeys: true });
    expect(extendedKeys({ TERM_PROGRAM: "ghostty" })).toEqual({ kitty: false, otherKeys: true });
    expect(extendedKeys({ TMUX: "x", TERM: "xterm-kitty" })).toEqual({
      kitty: false,
      otherKeys: true,
    });
    expect(extendedKeys({ TERM: "xterm-256color" })).toEqual({ kitty: false, otherKeys: false });
  });

  it("pops before it pushes so the kitty stack never grows", () => {
    const on = keyModesOn({ kitty: true, otherKeys: true });
    expect(on).toBe(`${seq.kittyPop}${seq.kittyPush(1)}${seq.otherKeysOn}`);
    expect(keyModesOff({ kitty: true, otherKeys: true })).toBe(
      `${seq.kittyPop}${seq.otherKeysOff}`,
    );
    expect(keyModesOn({ kitty: false, otherKeys: false })).toBe("");
  });

  it("turns keys, mouse and paste off around an external editor and back on after", () => {
    const caps = detectCapabilities({ TERM_PROGRAM: "WezTerm" });
    const { before, after } = editorHandoff(caps);
    expect(before).toContain(seq.kittyPop);
    expect(before).toContain(seq.otherKeysOff);
    expect(before).toContain(mouseOff("all"));
    expect(before).toContain(seq.pasteOff);
    expect(after).toContain(seq.kittyPush(1));
    expect(after).toContain(mouseOn("all"));
    expect(after.indexOf(seq.kittyPop)).toBeLessThan(after.indexOf(seq.kittyPush(1)));
  });
});

describe("mouse presets", () => {
  it("maps each preset to its modes", () => {
    expect(mouseOn("off")).toBe("");
    expect(mouseOn("wheel")).toBe("\u001b[?1000h\u001b[?1006h");
    expect(mouseOn("buttons")).toBe("\u001b[?1000h\u001b[?1002h\u001b[?1006h");
    expect(mouseOn("all")).toBe("\u001b[?1000h\u001b[?1002h\u001b[?1003h\u001b[?1006h");
    expect(mouseOff("all")).toBe("\u001b[?1006l\u001b[?1003l\u001b[?1002l\u001b[?1000l");
  });

  it("defaults to all, honours config, and lets INDUS_TUI_MOUSE force", () => {
    expect(mousePreset({})).toBe("all");
    expect(mousePreset({}, "wheel")).toBe("wheel");
    expect(mousePreset({}, "bogus")).toBe("all");
    expect(mousePreset({ INDUS_TUI_MOUSE: "buttons" }, "wheel")).toBe("buttons");
    expect(mousePreset({ INDUS_TUI_MOUSE: "0" }, "all")).toBe("off");
    expect(mousePreset({ INDUS_TUI_MOUSE: "on" }, "off")).toBe("all");
  });
});
