import { describe, expect, it } from "vitest";
import type { ComposerToken } from "../composer/composerStore.ts";
import { PromptQueue } from "./queue.ts";

const image: ComposerToken = { kind: "image", label: "[[image 1]]", path: "/a.png", name: "a.png" };

describe("prompt queue", () => {
  it("is first in, first out", () => {
    const queue = new PromptQueue();
    queue.add("a");
    queue.add("b");
    queue.add("c");
    expect([queue.shift()?.text, queue.shift()?.text, queue.shift()?.text]).toEqual([
      "a",
      "b",
      "c",
    ]);
    expect(queue.shift()).toBeUndefined();
  });

  it("marks image-bearing items and keeps only the tokens their text shows", () => {
    const queue = new PromptQueue();
    const other: ComposerToken = { kind: "paste", label: "[[paste 1 · 9 lines]]", text: "…" };
    const item = queue.add("look", "look [[image 1]]", [image, other]);
    expect(item.hasImage).toBe(true);
    expect(item.tokens).toEqual([image]);
    expect(queue.add("plain").hasImage).toBe(false);
  });

  it("promotes an edited item to the front and keeps an unchanged one as it was", () => {
    const queue = new PromptQueue();
    const a = queue.add("a");
    queue.add("b");
    queue.add("c");
    queue.setEditing(1);
    const edited = queue.takeEdited(1, "b2");
    expect(edited?.text).toBe("b2");
    expect(queue.editing).toBeNull();
    if (edited) queue.addFront(edited);
    expect(queue.items.map((item) => item.text)).toEqual(["b2", "a", "c"]);
    const same = queue.takeEdited(1, "a");
    expect(same).toBe(a);
  });

  it("keeps the edit index on the same item as the list moves", () => {
    const queue = new PromptQueue();
    queue.add("a");
    queue.add("b");
    queue.add("c");
    queue.setEditing(2);
    queue.shift();
    expect(queue.editing).toBe(1);
    queue.addFront(queue.make("z"));
    expect(queue.editing).toBe(2);
    queue.removeAt(0);
    expect(queue.items[queue.editing ?? -1]?.text).toBe("c");
    queue.removeAt(queue.editing ?? -1);
    expect(queue.editing).toBeNull();
  });

  it("refuses an edit index outside the list", () => {
    const queue = new PromptQueue();
    queue.add("only");
    queue.setEditing(3);
    expect(queue.editing).toBeNull();
    expect(queue.takeEdited(5, "x")).toBeUndefined();
  });

  it("tells subscribers about every change", () => {
    const queue = new PromptQueue();
    let seen = 0;
    const stop = queue.subscribe(() => {
      seen += 1;
    });
    queue.add("a");
    queue.shift();
    stop();
    queue.add("b");
    expect(seen).toBe(2);
  });
});
