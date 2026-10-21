import { describe, expect, it } from "vitest";
import { FLOOD_BREAKS, ResumeSanitizer, scrub, splitTail } from "./resumeSanitizer.ts";

function clock() {
  const state = { now: 1_000 };
  return { state, now: () => state.now };
}

const crlf = (n: number) => "\r\n".repeat(n);

describe("ResumeSanitizer", () => {
  it("folds 80 blank lines into one inside the 30 s window and not after", () => {
    const c = clock();
    const s = new ResumeSanitizer(c.now);
    s.begin();
    expect(s.feed(`head${crlf(81)}tail`)).toBe("head\r\n\r\ntail");

    c.state.now += 29_999;
    expect(s.active).toBe(true);
    expect(s.feed(`a${crlf(81)}b`)).toBe("a\r\n\r\nb");

    c.state.now += 1;
    expect(s.active).toBe(false);
    expect(s.feed(`a${crlf(81)}b`)).toBe(`a${crlf(81)}b`);
  });

  it("keeps erase sequences outside a flood (live repaints), inside the window too", () => {
    const c = clock();
    const s = new ResumeSanitizer(c.now);
    s.begin();
    expect(s.feed("one\x1b[Ktwo\x1b[2Xthree\x1b[0K.")).toBe("one\x1b[Ktwo\x1b[2Xthree\x1b[0K.");
    // Ink's repaint: erase the line, move up, erase, ... then the new frame.
    const repaint = "\x1b[2K\x1b[1A\x1b[2K\x1b[Gfresh\r\n\x1b[2Kline";
    expect(s.feed(repaint)).toBe(repaint);
    c.state.now += 30_000;
    expect(s.feed("spin\x1b[K.")).toBe("spin\x1b[K.");
  });

  it("drops the erase codes inside a flood with the flood", () => {
    const s = new ResumeSanitizer(clock().now);
    s.begin();
    expect(s.feed(`a\x1b[K${"\r\n\x1b[K".repeat(60)}b`)).toBe("a\r\n\r\nb");
  });

  it("passes everything through before any resume", () => {
    const s = new ResumeSanitizer(clock().now);
    const text = `x\x1b[K${crlf(70)}y`;
    expect(s.active).toBe(false);
    expect(s.feed(text)).toBe(text);
  });

  it("keeps runs below the threshold", () => {
    const s = new ResumeSanitizer(clock().now);
    s.begin();
    const text = `a${crlf(FLOOD_BREAKS - 1)}b`;
    expect(s.feed(text)).toBe(text);
  });

  it("holds an escape split across frames until it completes", () => {
    const s = new ResumeSanitizer(clock().now);
    s.begin();
    expect(s.feed("left\x1b")).toBe("left");
    expect(s.feed("[")).toBe("");
    expect(s.feed("2")).toBe("");
    expect(s.feed("Kright")).toBe("\x1b[2Kright");
    // An escape that turns out to be something else is passed on whole.
    expect(s.feed("x\x1b[")).toBe("x");
    expect(s.feed("31mred")).toBe("\x1b[31mred");
  });

  it("collects a flood split across frames, CR and LF split too", () => {
    const s = new ResumeSanitizer(clock().now);
    s.begin();
    let out = s.feed(`start${crlf(30)}\r`);
    out += s.feed(`\n${crlf(20)}`);
    out += s.feed(`${crlf(30)}end`);
    expect(out).toBe("start\r\n\r\nend");
  });

  it("collects a flood interrupted by erase codes", () => {
    const s = new ResumeSanitizer(clock().now);
    s.begin();
    // A complete erase code ending a frame stays held with the breaks before it.
    let out = s.feed(`z${crlf(30)}\x1b[K`);
    expect(out).toBe("z");
    out += s.feed(`${crlf(30)}a${crlf(30)}\x1b[`);
    out += s.feed(`K${crlf(30)}b`);
    out += s.feed(`${crlf(25)}\x1b[K${crlf(25)}c`);
    expect(out).toBe("z\r\n\r\na\r\n\r\nb\r\n\r\nc");
  });

  it("settle releases held breaks when the window ends; a held escape completes with the next frame", () => {
    const c = clock();
    const s = new ResumeSanitizer(c.now);
    s.begin();
    expect(s.feed(`line${crlf(3)}`)).toBe("line");
    c.state.now += 30_000;
    expect(s.settle()).toBe(crlf(3));

    s.begin();
    expect(s.feed("x\x1b[1")).toBe("x");
    c.state.now += 30_000;
    expect(s.settle()).toBe("");
    expect(s.feed("mbold")).toBe("\x1b[1mbold");
  });

  it("end drops a held partial escape and closes the window", () => {
    const s = new ResumeSanitizer(clock().now);
    s.begin();
    expect(s.feed(`a${crlf(2)}\x1b[`)).toBe("a");
    expect(s.end()).toBe(crlf(2));
    expect(s.active).toBe(false);
    expect(s.feed("K")).toBe("K");
  });

  it("begin while active extends the window, never shortens it", () => {
    const c = clock();
    const s = new ResumeSanitizer(c.now);
    s.begin();
    c.state.now += 20_000;
    s.begin();
    expect(s.remainingMs).toBe(30_000);
  });
});

describe("helpers", () => {
  it("splitTail holds a partial escape, the breaks before it, or trailing breaks", () => {
    expect(splitTail("abc")).toEqual(["abc", ""]);
    expect(splitTail("abc\r\n\r\n")).toEqual(["abc", "\r\n\r\n"]);
    expect(splitTail("abc\r")).toEqual(["abc", "\r"]);
    expect(splitTail("abc\r\n\x1b[12")).toEqual(["abc", "\r\n\x1b[12"]);
    expect(splitTail("abc\x1b[12m")).toEqual(["abc\x1b[12m", ""]);
    expect(splitTail("abc\r\n\x1b[K")).toEqual(["abc", "\r\n\x1b[K"]);
    expect(splitTail("spin\x1b[K")).toEqual(["spin\x1b[K", ""]);
  });

  it("scrub counts erase codes inside a run as part of it and keeps short runs whole", () => {
    expect(scrub(`${crlf(25)}\x1b[K${crlf(25)}`)).toBe("\r\n\r\n");
    expect(scrub(`x${crlf(3)}\x1b[Ky`)).toBe(`x${crlf(3)}\x1b[Ky`);
  });
});
