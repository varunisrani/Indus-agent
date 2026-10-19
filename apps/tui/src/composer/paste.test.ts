import { beforeEach, describe, expect, it } from "vitest";
import { captureDestination, isCurrent } from "../state/destination.ts";
import { uiStore } from "../state/uiStore.ts";
import type { Composer } from "./composerStore.ts";
import {
  cleanPaste,
  expandTokens,
  handlePaste,
  isImagePath,
  looksLikePath,
  type PasteDeps,
  pastedPath,
  pasteFromClipboard,
  pasteLabel,
} from "./paste.ts";
import { type FakeBridge, resetUi, settle, submitRig } from "./testSupport.ts";

function deps(bridge: FakeBridge, composer: Composer): PasteDeps {
  return {
    rpc: bridge.rpc,
    composer,
    sessionId: () => uiStore.getState().sid,
    capture: captureDestination,
    isCurrent,
    limits: () => ({ lines: uiStore.getState().pasteLines, chars: uiStore.getState().pasteChars }),
  };
}

/** A promise with its resolver, to hold a bridge answer while the test types. */
function held<T>(): { promise: Promise<T>; release: (value: T) => void } {
  let release: (value: T) => void = () => {};
  const promise = new Promise<T>((resolve) => {
    release = resolve;
  });
  return { promise, release };
}

beforeEach(() => resetUi());

describe("cleaning", () => {
  it("strips bracketed-paste markers and unifies line endings without flattening", () => {
    expect(cleanPaste("\u001b[200~one\r\ntwo\rthree\n\n\u001b[201~")).toBe("one\ntwo\nthree");
    expect(cleanPaste("\n\n")).toBe("\n\n");
  });
});

describe("looks like a path", () => {
  it.each([
    ["~/notes.md", true],
    ["./a", true],
    ["../b/c", true],
    ["/usr/local/bin", true],
    ["/etc.conf", true],
    ['"/Users/me/My Shot.png"', true],
    ["C:\\Users\\me\\a.png", true],
    ["file:///tmp/a%20b.png", true],
    ["/help", false],
    ["/api", false],
    ["hello world", false],
    ["two\nlines/", false],
  ])("%s → %s", (text, expected) => {
    expect(looksLikePath(text)).toBe(expected);
  });

  it("unwraps quotes and file URLs and spots image suffixes", () => {
    expect(pastedPath("file:///tmp/a%20b.png")).toBe("/tmp/a b.png");
    expect(pastedPath("'/x/y.JPG'")).toBe("/x/y.JPG");
    expect(isImagePath('"/x/y.JPG"')).toBe(true);
    expect(isImagePath("/x/y.txt")).toBe(false);
  });
});

describe("collapse", () => {
  it("collapses a 5-line paste to a label and submit still carries the full text", async () => {
    const rig = submitRig({
      "paste.collapse": () => ({
        placeholder: "[…]",
        path: "/home/u/.indus/pastes/paste_1.txt",
        lines: 5,
      }),
      "prompt.submit": () => ({ turn_id: "t1", generation: 1, status: "streaming" }),
    });
    const text = "l1\nl2\nl3\nl4\nl5";
    rig.composer.setText("see ");
    expect(await handlePaste(text, deps(rig.bridge, rig.composer))).toBe("collapsed");
    expect(rig.composer.text).toBe(`see ${pasteLabel(1, 5)}`);
    await settle();
    expect(rig.bridge.calls("paste.collapse")).toEqual([{ text }]);
    const token = rig.composer.tokens[0];
    expect(token?.kind === "paste" ? token.path : "").toBe("/home/u/.indus/pastes/paste_1.txt");
    rig.composer.insertText(" please");
    rig.submitter.submit();
    await settle();
    expect(rig.bridge.calls("prompt.submit")[0]?.text).toBe(`see ${text} please`);
    expect(rig.user).toEqual([`see ${pasteLabel(1, 5)} please`]);
    expect(rig.history).toEqual([`see ${text} please`]);
  });

  it("keeps a 4-line paste inline, with its newlines", async () => {
    const rig = submitRig();
    expect(await handlePaste("a\nb\nc\nd", deps(rig.bridge, rig.composer))).toBe("inline");
    expect(rig.composer.text).toBe("a\nb\nc\nd");
    expect(rig.composer.tokens).toEqual([]);
  });

  it("collapses on the character limit too, and the limits come from config", async () => {
    resetUi({ pasteChars: 10, pasteLines: 0 });
    const rig = submitRig({ "paste.collapse": () => ({ placeholder: "", path: "/p", lines: 1 }) });
    expect(await handlePaste("x".repeat(10), deps(rig.bridge, rig.composer))).toBe("collapsed");
    expect(await handlePaste("y".repeat(9), deps(rig.bridge, rig.composer))).toBe("inline");
  });

  it("drops a token whose label the user deleted", async () => {
    const rig = submitRig({ "paste.collapse": () => ({ placeholder: "", path: "/p", lines: 6 }) });
    await handlePaste("1\n2\n3\n4\n5\n6", deps(rig.bridge, rig.composer));
    expect(rig.composer.tokens).toHaveLength(1);
    rig.composer.setText("");
    expect(rig.composer.tokens).toHaveLength(0);
  });
});

describe("paths", () => {
  it("stages a pasted image path and shows an image token", async () => {
    const rig = submitRig({
      "image.attach": (params) => ({
        name: "shot.png",
        path: `/abs${String(params.path)}`,
        attached: 1,
      }),
    });
    expect(await handlePaste("/tmp/shot.png", deps(rig.bridge, rig.composer))).toBe("image");
    expect(rig.bridge.calls("image.attach")).toEqual([{ session_id: "s1", path: "/tmp/shot.png" }]);
    expect(rig.composer.text).toBe("[[image 1]]");
    expect(rig.composer.tokens[0]).toMatchObject({ kind: "image", path: "/abs/tmp/shot.png" });
    expect(expandTokens("look [[image 1]]", rig.composer.tokens)).toBe("look");
  });

  it("inserts the resolved paths of a dropped file", async () => {
    const rig = submitRig({
      "input.detect_drop": () => ({ is_drop: true, paths: ["/home/u/My Notes.md"], images: [] }),
    });
    expect(await handlePaste("~/My\\ Notes.md", deps(rig.bridge, rig.composer))).toBe("drop");
    expect(rig.composer.text).toBe('"/home/u/My Notes.md"');
  });

  it("drop with images: attaches each image and keeps the other paths as text", async () => {
    const rig = submitRig({
      "image.attach": (params) => ({ name: "a.png", path: params.path, attached: 1 }),
      "input.detect_drop": () => ({
        is_drop: true,
        paths: ["/d/a.png", "/d/b.txt"],
        images: ["/d/a.png"],
      }),
    });
    expect(await handlePaste("/d/a.png /d/b.txt", deps(rig.bridge, rig.composer))).toBe("drop");
    expect(rig.composer.text).toBe("[[image 1]] /d/b.txt");
  });

  it("keeps the raw text when the user types while drop detection runs", async () => {
    const answer = held<unknown>();
    const rig = submitRig({ "input.detect_drop": () => answer.promise });
    const pending = handlePaste("./src/main.ts", deps(rig.bridge, rig.composer));
    await settle();
    rig.composer.insertText("typed ");
    answer.release({ is_drop: true, paths: ["/abs/src/main.ts"], images: [] });
    expect(await pending).toBe("inline");
    expect(rig.composer.text).toBe("typed ./src/main.ts");
  });

  it("drops the result when the session changed meanwhile", async () => {
    const answer = held<unknown>();
    const rig = submitRig({ "input.detect_drop": () => answer.promise });
    const pending = handlePaste("./src/main.ts", deps(rig.bridge, rig.composer));
    await settle();
    uiStore.getState().patch({ sid: "s2", storedSid: "s2" });
    answer.release({ is_drop: true, paths: ["/abs/src/main.ts"], images: [] });
    expect(await pending).toBe("stale");
    expect(rig.composer.text).toBe("");
  });

  it("falls back to plain text when drop detection fails", async () => {
    const rig = submitRig({
      "input.detect_drop": () => {
        throw new Error("bridge down");
      },
    });
    expect(await handlePaste("../x/y", deps(rig.bridge, rig.composer))).toBe("inline");
    expect(rig.composer.text).toBe("../x/y");
  });

  it("asks nothing without a session", async () => {
    resetUi({ sid: null, storedSid: null });
    const rig = submitRig();
    expect(await handlePaste("/tmp/a.png", deps(rig.bridge, rig.composer))).toBe("inline");
    expect(rig.bridge.calls("image.attach")).toEqual([]);
  });
});

describe("clipboard paste", () => {
  it("puts clipboard text through the paste path, or says the clipboard is empty", async () => {
    const rig = submitRig();
    const notes: string[] = [];
    const paste = deps(rig.bridge, rig.composer);
    expect(
      await pasteFromClipboard(
        paste,
        async () => "from the clipboard",
        (t) => notes.push(t),
      ),
    ).toBe("inline");
    expect(rig.composer.text).toBe("from the clipboard");
    expect(
      await pasteFromClipboard(
        paste,
        async () => null,
        (t) => notes.push(t),
      ),
    ).toBe("empty");
    expect(notes).toEqual(["the clipboard holds no text"]);
  });

  it("drops the text when the session changed while the clipboard was read", async () => {
    const rig = submitRig();
    const outcome = pasteFromClipboard(
      deps(rig.bridge, rig.composer),
      async () => {
        uiStore.getState().patch({ sid: "other", storedSid: "other" });
        return "late";
      },
      () => {},
    );
    expect(await outcome).toBe("stale");
    expect(rig.composer.text).toBe("");
  });
});
