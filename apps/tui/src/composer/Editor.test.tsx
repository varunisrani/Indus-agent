import { afterEach, describe, expect, it } from "vitest";
import { Composer } from "./composerStore.ts";
import { Editor } from "./Editor.tsx";
import { type InkHarness, renderInk } from "./testSupport.ts";

let harness: InkHarness | null = null;

afterEach(() => {
  harness?.unmount();
  harness = null;
});

describe("Editor", () => {
  it("shows the placeholder while empty and the typed text afterwards", async () => {
    const composer = new Composer();
    harness = await renderInk(<Editor composer={composer} placeholder="Ask anything" />);
    expect(harness.frame()).toContain("Ask anything");
    await harness.type("hi there");
    expect(composer.text).toBe("hi there");
    expect(harness.frame()).toContain("hi there");
    expect(harness.frame()).not.toContain("Ask anything");
  });

  it("calls onSubmit on Enter and inserts a newline on Alt+Enter", async () => {
    const composer = new Composer();
    const sent: string[] = [];
    harness = await renderInk(<Editor composer={composer} onSubmit={(text) => sent.push(text)} />);
    await harness.type("line one");
    await harness.type("\u001b\r");
    await harness.type("line two");
    expect(composer.text).toBe("line one\nline two");
    await harness.type("\r");
    expect(sent).toEqual(["line one\nline two"]);
    const frame = harness.frame();
    expect(frame).toContain("line one");
    expect(frame).toContain("line two");
  });

  it("edits by grapheme and reports the top edge to its owner", async () => {
    const composer = new Composer();
    const edges: string[] = [];
    harness = await renderInk(<Editor composer={composer} onEdge={(dir) => edges.push(dir)} />);
    await harness.type("ok 👍🏽");
    await harness.type("\u007f");
    expect(composer.text).toBe("ok ");
    await harness.type("\u001b[A");
    expect(edges).toEqual(["up"]);
  });

  it("draws continuation lines above the live line", async () => {
    const composer = new Composer();
    composer.pushContinuation("earlier line");
    composer.setText("now");
    harness = await renderInk(<Editor composer={composer} />);
    const lines = harness.frame().split("\n");
    const earlier = lines.findIndex((line) => line.includes("earlier line"));
    const now = lines.findIndex((line) => line.includes("now"));
    expect(earlier).toBeGreaterThanOrEqual(0);
    expect(now).toBeGreaterThan(earlier);
  });

  it("places the cursor with a mouse press", async () => {
    const composer = new Composer();
    composer.setText("click here");
    const mouse = { current: null as null | import("./Editor.tsx").EditorMouse };
    harness = await renderInk(<Editor composer={composer} mouseRef={mouse} width={40} />);
    mouse.current?.press(0, 6, 1_000);
    expect(composer.editor.cursor).toBe(6);
  });
});
