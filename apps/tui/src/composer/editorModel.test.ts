import { describe, expect, it } from "vitest";
import { press, textPress } from "../input/keymap.ts";
import {
  decideKey,
  type KeyMemory,
  type KeyView,
  performKey,
  snapshotView,
} from "../input/routeKeys.ts";
import { Composer } from "./composerStore.ts";
import {
  applyKey,
  boundaries,
  cellWidth,
  EditorModel,
  offsetAt,
  UNDO_LIMIT,
} from "./editorModel.ts";

const FAMILY = "\u{1F468}‍\u{1F469}‍\u{1F467}"; // one grapheme, five code points
const THUMB = "\u{1F44D}\u{1F3FD}"; // thumbs up with a skin tone
const E_ACUTE = "é"; // e + combining acute
const CJK = "漢字";

function model(text: string, cursor = text.length): EditorModel {
  const m = new EditorModel();
  m.load(text, cursor);
  return m;
}

function keys(m: EditorModel, ...specs: string[]): void {
  for (const spec of specs) applyKey(m, press(spec), false);
}

describe("graphemes", () => {
  it("steps over emoji sequences, combining marks and CJK as single units", () => {
    for (const unit of [FAMILY, THUMB, E_ACUTE, "漢"]) {
      const m = model(`a${unit}b`, 1);
      keys(m, "right");
      expect(m.cursor).toBe(1 + unit.length);
      keys(m, "left");
      expect(m.cursor).toBe(1);
      keys(m, "delete");
      expect(m.text).toBe("ab");
      m.load(`a${unit}`);
      keys(m, "backspace");
      expect(m.text).toBe("a");
    }
  });

  it("snaps an outside cursor onto a boundary", () => {
    const m = model(FAMILY, 3);
    expect(m.cursor).toBe(0);
    expect(boundaries(`x${THUMB}`)).toEqual([0, 1, 1 + THUMB.length]);
  });

  it("types a combining mark onto a letter and lands after it", () => {
    const m = model("cafe");
    applyKey(m, textPress("́"), false);
    expect(m.text).toBe(`caf${E_ACUTE}`);
    expect(m.cursor).toBe(m.text.length);
    keys(m, "backspace");
    expect(m.text).toBe("caf");
  });

  it("measures wide cells and maps screen cells back to offsets", () => {
    expect(cellWidth("漢")).toBe(2);
    expect(cellWidth(THUMB)).toBe(2);
    expect(cellWidth("a")).toBe(1);
    expect(offsetAt(`${CJK}ab`, 0, 3, 80)).toBe(1);
    expect(offsetAt("abc\ndef", 1, 1, 80)).toBe(5);
    expect(offsetAt("abc\ndef", 0, 40, 80)).toBe(3);
    expect(offsetAt("abcdef", 1, 0, 4)).toBe(4);
  });
});

describe("plain keys", () => {
  it("inserts text, multi-character input as one undo step", () => {
    const m = model("");
    applyKey(m, textPress("h"), false);
    applyKey(m, textPress("ello wörld"), false);
    expect(m.text).toBe("hello wörld");
    expect(m.undoDepth).toBe(2);
    keys(m, "ctrl+z");
    expect(m.text).toBe("h");
  });

  it("moves to the line edges with Home/End and Ctrl+A/Ctrl+E", () => {
    const m = model("one\ntwo three", 6);
    keys(m, "home");
    expect(m.cursor).toBe(4);
    keys(m, "end");
    expect(m.cursor).toBe(13);
    keys(m, "ctrl+a");
    expect(m.cursor).toBe(4);
    keys(m, "ctrl+e");
    expect(m.cursor).toBe(13);
  });

  it("selects everything with Cmd+A on macOS (Ctrl+A stays line start)", () => {
    const m = model("one\ntwo", 5);
    applyKey(m, press("cmd+a"), true);
    expect(m.selectedText()).toBe("one\ntwo");
    applyKey(m, press("ctrl+a"), true);
    expect(m.cursor).toBe(4);
  });

  it("ignores chords it does not own", () => {
    const m = model("abc");
    expect(applyKey(m, press("ctrl+t"), false)).toBe("ignored");
    expect(applyKey(m, press("tab"), false)).toBe("ignored");
    expect(applyKey(m, press("alt+x"), false)).toBe("ignored");
    expect(m.text).toBe("abc");
  });
});

describe("enter", () => {
  it("submits on Enter and inserts a newline on Shift/Alt+Enter and a bare LF", () => {
    const m = model("hi");
    expect(applyKey(m, press("enter"), false)).toBe("submit");
    expect(applyKey(m, press("shift+enter"), false)).toBe("edited");
    expect(applyKey(m, press("alt+enter"), false)).toBe("edited");
    expect(applyKey(m, textPress("\n"), false)).toBe("edited");
    expect(m.text).toBe("hi\n\n\n");
  });

  it("keeps IME text committed together with the carriage return, then submits", () => {
    const m = model("");
    expect(applyKey(m, textPress("会议\r"), false)).toBe("submit");
    expect(m.text).toBe("会议");
  });
});

describe("word motion", () => {
  it("moves by whitespace-delimited words with Ctrl/Meta arrows and Meta+B/F", () => {
    const m = model("alpha  beta gamma", 17);
    keys(m, "ctrl+left");
    expect(m.cursor).toBe(12);
    keys(m, "alt+left");
    expect(m.cursor).toBe(7);
    keys(m, "alt+b");
    expect(m.cursor).toBe(0);
    keys(m, "alt+f");
    expect(m.cursor).toBe(7);
    keys(m, "ctrl+right");
    expect(m.cursor).toBe(12);
  });
});

describe("word deletes", () => {
  it("deletes the previous word with Ctrl+W, Ctrl+Backspace and Meta+Backspace", () => {
    for (const spec of ["ctrl+w", "ctrl+backspace", "alt+backspace"]) {
      const m = model("say hello  ");
      keys(m, spec);
      expect(m.text).toBe("say ");
    }
  });

  it("deletes the next word with Meta+D, Ctrl+Delete and Meta+Delete", () => {
    for (const spec of ["alt+d", "ctrl+delete", "alt+delete"]) {
      const m = model("say hello there", 4);
      keys(m, spec);
      expect(m.text).toBe("say there");
      expect(m.cursor).toBe(4);
    }
  });
});

describe("line kills", () => {
  it("Ctrl+U kills to the line start and, repeated, walks up across lines", () => {
    const m = model("first\nsecond");
    keys(m, "ctrl+u");
    expect(m.text).toBe("first\n");
    keys(m, "ctrl+u");
    expect(m.text).toBe("first");
    keys(m, "ctrl+u");
    expect(m.text).toBe("");
  });

  it("Ctrl+K kills to the line end and, at a line end, joins the next line", () => {
    const m = model("ab\ncd", 1);
    keys(m, "ctrl+k");
    expect(m.text).toBe("a\ncd");
    keys(m, "ctrl+k");
    expect(m.text).toBe("acd");
  });

  it("Cmd+Backspace / Cmd+Delete kill to the line edges", () => {
    const m = model("one two", 4);
    keys(m, "cmd+backspace");
    expect(m.text).toBe("two");
    m.load("one two", 3);
    keys(m, "cmd+delete");
    expect(m.text).toBe("one");
  });

  it("does nothing (and records nothing) on empty text", () => {
    const m = model("");
    keys(m, "backspace", "ctrl+u", "ctrl+w", "delete", "ctrl+k");
    expect(m.undoDepth).toBe(0);
  });
});

describe("line navigation", () => {
  it("keeps the column between lines and reports the edges", () => {
    const m = model("abcd\nxy\n漢字ab", 3);
    expect(applyKey(m, press("down"), false)).toBe("moved");
    expect(m.cursor).toBe(7);
    expect(applyKey(m, press("down"), false)).toBe("moved");
    expect(m.cursor).toBe(10);
    expect(applyKey(m, press("down"), false)).toBe("edge-down");
    m.load("abcd\nxy", 1);
    expect(applyKey(m, press("up"), false)).toBe("edge-up");
  });
});

describe("undo", () => {
  it(`keeps ${UNDO_LIMIT} snapshots: the 201st edit drops the oldest`, () => {
    const m = model("");
    for (let i = 0; i < UNDO_LIMIT + 1; i += 1) applyKey(m, textPress("x"), false);
    expect(m.undoDepth).toBe(UNDO_LIMIT);
    while (m.undo()) {
      // walk all the way back
    }
    expect(m.text).toBe("x");
  });

  it("redoes with Ctrl+Y or Ctrl+Shift+Z, and a new edit clears redo", () => {
    const m = model("");
    applyKey(m, textPress("a"), false);
    applyKey(m, textPress("b"), false);
    keys(m, "ctrl+z", "ctrl+z");
    expect(m.text).toBe("");
    keys(m, "ctrl+y");
    expect(m.text).toBe("a");
    keys(m, "ctrl+shift+z");
    expect(m.text).toBe("ab");
    keys(m, "ctrl+z");
    expect(m.redoDepth).toBe(1);
    applyKey(m, textPress("c"), false);
    expect(m.redoDepth).toBe(0);
    expect(m.text).toBe("ac");
  });

  it("starts afresh when the text is replaced from outside", () => {
    const m = model("");
    applyKey(m, textPress("a"), false);
    m.load("recalled");
    expect(m.undoDepth).toBe(0);
    expect(m.undo()).toBe(false);
  });
});

describe("selection", () => {
  it("extends with Shift, is replaced by typing and removed by Backspace", () => {
    const m = model("hello world", 0);
    keys(m, "shift+right", "shift+right");
    expect(m.selectedText()).toBe("he");
    applyKey(m, textPress("J"), false);
    expect(m.text).toBe("Jllo world");
    keys(m, "shift+end");
    expect(m.selectedText()).toBe("llo world");
    keys(m, "backspace");
    expect(m.text).toBe("J");
  });

  it("collapses to its side on a plain arrow", () => {
    const m = model("abcdef", 1);
    keys(m, "shift+right", "shift+right", "shift+right");
    keys(m, "left");
    expect(m.cursor).toBe(1);
    expect(m.range()).toBeNull();
  });

  it("selects the word on a double click and the line on a triple click within 500 ms", () => {
    const m = model("one two\nthree");
    expect(m.press(5, 1000)).toBe(1);
    expect(m.press(5, 1400)).toBe(2);
    expect(m.selectedText()).toBe("two");
    expect(m.press(5, 1800)).toBe(3);
    expect(m.selectedText()).toBe("one two");
    expect(m.press(5, 2400)).toBe(1);
    expect(m.range()).toBeNull();
  });

  it("drags a selection from the press point", () => {
    const m = model("drag me");
    m.press(0, 0);
    m.drag(4);
    expect(m.selectedText()).toBe("drag");
    m.release();
    m.drag(7);
    expect(m.selectedText()).toBe("drag");
  });
});

describe("esc esc", () => {
  const view = (composer: Composer): KeyView =>
    snapshotView(
      {
        busy: false,
        sid: "s1",
      } as Parameters<typeof snapshotView>[0],
      null,
      composer,
      { length: 0, editing: null },
      24,
    );

  function run(gap: number): { composer: Composer; history: string[] } {
    const composer = new Composer();
    composer.setText("draft to keep");
    const history: string[] = [];
    const memory: KeyMemory = { lastEsc: Number.NEGATIVE_INFINITY };
    const actions = {
      composer,
      history: { entries: () => history, push: (text: string) => history.push(text) },
      submitter: { clearDraft: () => composer.clear() },
    } as unknown as Parameters<typeof performKey>[3];
    for (const at of [10_000, 10_000 + gap]) {
      const k = press("esc");
      const v = view(composer);
      performKey(decideKey(k, v, memory, at, false), k, v, actions);
    }
    return { composer, history };
  }

  it("discards the draft into history when the second Esc comes within 500 ms", () => {
    const { composer, history } = run(500);
    expect(composer.text).toBe("");
    expect(history).toEqual(["draft to keep"]);
  });

  it("keeps the draft when the second Esc comes at 501 ms", () => {
    const { composer, history } = run(501);
    expect(composer.text).toBe("draft to keep");
    expect(history).toEqual([]);
  });
});
