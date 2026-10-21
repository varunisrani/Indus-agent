import { describe, expect, it, vi } from "vitest";
import type { MenuItem } from "./appMenu.ts";
import {
  type ContextMenuParamsLike,
  contextMenuTemplate,
  installContextMenus,
  isLinuxFindShortcut,
  type KeyInputLike,
  type MenuContentsLike,
} from "./contextMenu.ts";

type Listener = (...args: unknown[]) => void;

function fakeContents(id = 1) {
  const listeners = new Map<string, Listener[]>();
  const calls: string[] = [];
  const contents = {
    id,
    on: (event: string, listener: Listener) => {
      listeners.set(event, [...(listeners.get(event) ?? []), listener]);
    },
    replaceMisspelling: (word: string) => calls.push(`replace:${word}`),
    cut: () => calls.push("cut"),
    copy: () => calls.push("copy"),
    paste: () => calls.push("paste"),
    selectAll: () => calls.push("selectAll"),
    copyImageAt: (x: number, y: number) => calls.push(`copyImage:${x},${y}`),
    inspectElement: (x: number, y: number) => calls.push(`inspect:${x},${y}`),
    isDestroyed: () => false,
    session: {
      addWordToSpellCheckerDictionary: (word: string) => {
        calls.push(`learn:${word}`);
        return true;
      },
    },
  };
  const fire = (event: string, ...args: unknown[]) => {
    for (const listener of listeners.get(event) ?? []) listener(...args);
  };
  return { contents: contents as unknown as MenuContentsLike, calls, fire };
}

function params(overrides: Partial<ContextMenuParamsLike> = {}): ContextMenuParamsLike {
  return {
    x: 10,
    y: 20,
    isEditable: false,
    misspelledWord: "",
    dictionarySuggestions: [],
    selectionText: "",
    linkURL: "",
    mediaType: "none",
    editFlags: { canCut: false, canCopy: false, canPaste: false, canSelectAll: false },
    ...overrides,
  };
}

const labels = (menu: MenuItem[]) => menu.map((item) => item.label ?? `-${item.type}`);
const click = (menu: MenuItem[], label: string) => menu.find((i) => i.label === label)?.click?.();

describe("context menu", () => {
  it("offers spelling suggestions (at most five) and Add to Dictionary in an editable field", () => {
    const { contents, calls } = fakeContents();
    const menu = contextMenuTemplate(
      params({
        isEditable: true,
        misspelledWord: "teh",
        dictionarySuggestions: ["the", "ten", "tea", "tech", "eh", "heh"],
        editFlags: { canCut: true, canCopy: true, canPaste: true, canSelectAll: true },
      }),
      contents,
      { dev: false, writeText: () => {} },
    );
    expect(labels(menu)).toEqual([
      "the",
      "ten",
      "tea",
      "tech",
      "eh",
      "Add to Dictionary",
      "-separator",
      "Cut",
      "Copy",
      "Paste",
      "Select All",
    ]);
    click(menu, "tea");
    click(menu, "Add to Dictionary");
    click(menu, "Paste");
    expect(calls).toEqual(["replace:tea", "learn:teh", "paste"]);
  });

  it("says so when Chromium has no suggestion", () => {
    const { contents } = fakeContents();
    const menu = contextMenuTemplate(
      params({ isEditable: true, misspelledWord: "zzqx" }),
      contents,
      { dev: false, writeText: () => {} },
    );
    expect(menu[0]).toEqual({ label: "No Spelling Suggestions", enabled: false });
  });

  it("offers Copy for selected read-only text and nothing for an empty spot", () => {
    const { contents } = fakeContents();
    const options = { dev: false, writeText: () => {} };
    const flags = { canCut: true, canCopy: true, canPaste: true, canSelectAll: true };
    expect(
      labels(
        contextMenuTemplate(params({ selectionText: "hi", editFlags: flags }), contents, options),
      ),
    ).toEqual(["Copy"]);
    expect(contextMenuTemplate(params(), contents, options)).toEqual([]);
  });

  it("copies http, https and mailto links as text and never offers to open one", () => {
    const { contents } = fakeContents();
    const writeText = vi.fn();
    for (const link of ["https://example.com/a", "http://x.test", "mailto:a@example.com"]) {
      const menu = contextMenuTemplate(params({ linkURL: link }), contents, {
        dev: false,
        writeText,
      });
      expect(labels(menu)).toEqual(["Copy Link"]);
      click(menu, "Copy Link");
    }
    expect(writeText.mock.calls.map((c) => c[0])).toEqual([
      "https://example.com/a",
      "http://x.test",
      "mailto:a@example.com",
    ]);
    for (const link of ["javascript:alert(1)", "file:///etc/passwd", "indus://open/x"]) {
      expect(
        contextMenuTemplate(params({ linkURL: link }), contents, { dev: false, writeText }),
      ).toEqual([]);
    }
  });

  it("copies an image at the click point; Inspect Element only in dev", () => {
    const { contents, calls } = fakeContents();
    const menu = contextMenuTemplate(params({ mediaType: "image" }), contents, {
      dev: true,
      writeText: () => {},
    });
    expect(labels(menu)).toEqual(["Copy Image", "-separator", "Inspect Element"]);
    click(menu, "Copy Image");
    click(menu, "Inspect Element");
    expect(calls).toEqual(["copyImage:10,20", "inspect:10,20"]);
    expect(
      labels(
        contextMenuTemplate(params({ mediaType: "image" }), contents, {
          dev: false,
          writeText: () => {},
        }),
      ),
    ).toEqual(["Copy Image"]);
  });
});

function key(overrides: Partial<KeyInputLike> = {}): KeyInputLike {
  return {
    type: "keyDown",
    key: "f",
    control: true,
    meta: false,
    alt: false,
    shift: false,
    ...overrides,
  };
}

describe("Linux find shortcut", () => {
  it("is Ctrl+F alone on Linux only", () => {
    expect(isLinuxFindShortcut(key(), "linux")).toBe(true);
    expect(isLinuxFindShortcut(key({ key: "F" }), "linux")).toBe(true);
    expect(isLinuxFindShortcut(key(), "darwin")).toBe(false);
    expect(isLinuxFindShortcut(key(), "win32")).toBe(false);
    expect(isLinuxFindShortcut(key({ shift: true }), "linux")).toBe(false);
    expect(isLinuxFindShortcut(key({ alt: true }), "linux")).toBe(false);
    expect(isLinuxFindShortcut(key({ type: "keyUp" }), "linux")).toBe(false);
    expect(isLinuxFindShortcut(key({ isAutoRepeat: true }), "linux")).toBe(false);
  });

  function install(platform: string, appWindow: boolean) {
    const created: Array<(c: MenuContentsLike) => void> = [];
    const opened: number[] = [];
    const popups: string[][] = [];
    installContextMenus({
      dev: false,
      platform,
      onWebContentsCreated: (listener) => created.push(listener),
      popup: (template) => popups.push(labels(template)),
      writeText: () => {},
      isAppWindow: () => appWindow,
      emitFindOpen: (contents) => opened.push(contents.id),
    });
    const fake = fakeContents(9);
    for (const listener of created) listener(fake.contents);
    return { fake, opened, popups };
  }

  it("is claimed in app windows and opens that window's find bar", () => {
    const { fake, opened } = install("linux", true);
    const event = { preventDefault: vi.fn() };
    fake.fire("before-input-event", event, key());
    expect(event.preventDefault).toHaveBeenCalledOnce();
    expect(opened).toEqual([9]);
    const other = { preventDefault: vi.fn() };
    fake.fire("before-input-event", other, key({ key: "g" }));
    expect(other.preventDefault).not.toHaveBeenCalled();
  });

  it("is left alone in preview guests and on macOS/Windows", () => {
    for (const [platform, appWindow] of [
      ["linux", false],
      ["darwin", true],
      ["win32", true],
    ] as const) {
      const { fake, opened } = install(platform, appWindow);
      const event = { preventDefault: vi.fn() };
      fake.fire("before-input-event", event, key());
      expect(event.preventDefault).not.toHaveBeenCalled();
      expect(opened).toEqual([]);
    }
  });

  it("every created web contents gets the context menu", () => {
    const { fake, popups } = install("darwin", false);
    fake.fire(
      "context-menu",
      {},
      params({
        selectionText: "x",
        editFlags: { canCut: false, canCopy: true, canPaste: false, canSelectAll: false },
      }),
    );
    fake.fire("context-menu", {}, params());
    expect(popups).toEqual([["Copy"]]);
  });
});
