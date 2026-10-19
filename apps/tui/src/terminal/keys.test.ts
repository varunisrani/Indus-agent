import { describe, expect, it, vi } from "vitest";
import { InputFilter, type MouseEvent, rewriteInput } from "./inputFilter.ts";
import { decodeKey, decodeModifiers, keyLabel } from "./keys.ts";

const label = (bytes: string): string | null => {
  const event = decodeKey(bytes);
  return event === null ? null : keyLabel(event);
};

describe("key table", () => {
  const rows: [string, string, string][] = [
    ["Shift+Enter (kitty)", "\u001b[13;2u", "shift+enter"],
    ["Shift+Enter (modifyOtherKeys)", "\u001b[27;2;13~", "shift+enter"],
    ["Alt+Enter (kitty)", "\u001b[13;3u", "alt+enter"],
    ["Alt+Enter (modifyOtherKeys)", "\u001b[27;3;13~", "alt+enter"],
    ["Alt+Enter (classic)", "\u001b\r", "alt+enter"],
    ["Ctrl+W (kitty)", "\u001b[119;5u", "ctrl+w"],
    ["Ctrl+W (modifyOtherKeys)", "\u001b[27;5;119~", "ctrl+w"],
    ["Ctrl+W (classic)", "\u0017", "ctrl+w"],
    ["Ctrl+Backspace (kitty)", "\u001b[127;5u", "ctrl+backspace"],
    ["Ctrl+Backspace (modifyOtherKeys)", "\u001b[27;5;127~", "ctrl+backspace"],
    ["Ctrl+Backspace (classic ^H)", "\b", "ctrl+backspace"],
    ["Alt+Backspace (classic)", "\u001b\u007f", "alt+backspace"],
    ["plain Enter", "\r", "enter"],
    ["plain Backspace", "\u007f", "backspace"],
    ["Escape (kitty)", "\u001b[27u", "escape"],
    ["Shift+Tab (kitty)", "\u001b[9;2u", "shift+tab"],
    ["Ctrl+Shift+A (kitty)", "\u001b[97;6u", "ctrl+shift+a"],
    ["Super+K (kitty)", "\u001b[107;9u", "super+k"],
    ["Up", "\u001b[A", "up"],
    ["Ctrl+Left", "\u001b[1;5D", "ctrl+left"],
    ["PageDown", "\u001b[6~", "pagedown"],
    ["Shift+Delete", "\u001b[3;2~", "shift+delete"],
    ["letter", "q", "q"],
    ["emoji", "🙂", "🙂"],
  ];
  for (const [name, bytes, expected] of rows) {
    it(name, () => {
      expect(label(bytes)).toBe(expected);
    });
  }

  it("reads the modifier bits", () => {
    expect(decodeModifiers(1)).toEqual({ shift: false, alt: false, ctrl: false, super: false });
    expect(decodeModifiers(16)).toEqual({ shift: true, alt: true, ctrl: true, super: true });
  });

  it("returns null for what it does not know", () => {
    expect(decodeKey("")).toBeNull();
    expect(decodeKey("\u001b[99Z")).toBeNull();
    expect(decodeKey("abc")).toBeNull();
  });
});

describe("input filter", () => {
  it("rewrites modifyOtherKeys into the CSI-u form and lifts mouse reports out", () => {
    const mice: MouseEvent[] = [];
    const out = rewriteInput("a\u001b[27;2;13~\u001b[<64;10;5Mb\u001b[<0;3;4m", (m) =>
      mice.push(m),
    );
    expect(out).toBe("a\u001b[13;2ub");
    expect(mice.map((m) => `${m.kind}@${m.col},${m.row}`)).toEqual([
      "wheel-up@10,5",
      "release@3,4",
    ]);
  });

  it("joins sequences split across reads and leaves paste bodies alone", async () => {
    const { PassThrough } = await import("node:stream");
    const source = new PassThrough();
    const mice: MouseEvent[] = [];
    const filter = new InputFilter(source, (m) => mice.push(m));
    let seen = "";
    filter.stream.on("data", (chunk: Buffer) => {
      seen += String(chunk);
    });
    filter.push("x\u001b[27;5");
    filter.push(";119~y");
    filter.push("\u001b[200~keep \u001b[27;2;13~ as typed");
    filter.push("\u001b[201~");
    filter.push("\u001b[<32;7;");
    filter.push("8M");
    await new Promise((resolve) => setImmediate(resolve));
    expect(seen).toBe("x\u001b[119;5uy\u001b[200~keep \u001b[27;2;13~ as typed\u001b[201~");
    expect(mice.map((m) => m.kind)).toEqual(["drag"]);
  });

  it("lets a lone ESC through after a short pause", async () => {
    vi.useFakeTimers();
    const { PassThrough } = await import("node:stream");
    const filter = new InputFilter(new PassThrough(), () => {});
    let seen = "";
    filter.stream.on("data", (chunk: Buffer) => {
      seen += String(chunk);
    });
    filter.push("\u001b");
    expect(seen).toBe("");
    vi.advanceTimersByTime(20);
    await vi.runAllTimersAsync();
    vi.useRealTimers();
    await new Promise((resolve) => setImmediate(resolve));
    expect(seen).toBe("\u001b");
  });
});
