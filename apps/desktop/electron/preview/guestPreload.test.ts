// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";
import { GUEST_LINK_CHANNEL, installLinkHandOff, linkToHandOff } from "./guestPreload.ts";

const electron = vi.hoisted(() => ({
  sendToHost: vi.fn(),
  exposeInMainWorld: vi.fn(),
}));

vi.mock("electron", () => ({
  ipcRenderer: { sendToHost: electron.sendToHost },
  contextBridge: { exposeInMainWorld: electron.exposeInMainWorld },
}));

beforeEach(() => {
  document.body.innerHTML = `
    <a id="blank" href="/docs?x=1" target="_blank"><span id="inner">Docs</span></a>
    <a id="BLANK" href="https://example.com/caps" target="_BLANK">Caps</a>
    <a id="plain" href="https://example.com/plain">Plain</a>
    <a id="file" href="file:///etc/passwd" target="_blank">File</a>
    <div id="nolink">no link</div>`;
});

function el(id: string): Element {
  const found = document.getElementById(id);
  if (found === null) throw new Error(id);
  return found;
}

describe("guest link hand-off", () => {
  it("forwards a trusted primary click on a _blank link (and on a child of it)", () => {
    const base = window.location.href;
    expect(linkToHandOff({ isTrusted: true, button: 0, target: el("blank") })).toBe(
      new URL("/docs?x=1", base).href,
    );
    expect(linkToHandOff({ isTrusted: true, button: 0, target: el("inner") })).toBe(
      new URL("/docs?x=1", base).href,
    );
    expect(linkToHandOff({ isTrusted: true, target: el("BLANK") })).toBe(
      "https://example.com/caps",
    );
    // the scheme is main's business (it refuses file:); the preload only reports what was clicked
    expect(linkToHandOff({ isTrusted: true, button: 0, target: el("file") })).toBe(
      "file:///etc/passwd",
    );
  });

  it("drops synthetic, non-primary and plain-link clicks", () => {
    expect(linkToHandOff({ isTrusted: false, button: 0, target: el("blank") })).toBeNull();
    expect(linkToHandOff({ button: 0, target: el("blank") })).toBeNull();
    expect(linkToHandOff({ isTrusted: true, button: 1, target: el("blank") })).toBeNull();
    expect(linkToHandOff({ isTrusted: true, button: 2, target: el("blank") })).toBeNull();
    expect(linkToHandOff({ isTrusted: true, button: 0, target: el("plain") })).toBeNull();
    expect(linkToHandOff({ isTrusted: true, button: 0, target: el("nolink") })).toBeNull();
    expect(linkToHandOff({ isTrusted: true, button: 0, target: null })).toBeNull();
    expect(linkToHandOff({ isTrusted: true, button: 0, target: { closest: 1 } })).toBeNull();
  });

  it("a page's own click() on a _blank link sends nothing to the host", () => {
    const send = vi.fn();
    installLinkHandOff(document, send);
    (el("blank") as HTMLAnchorElement).click();
    el("blank").dispatchEvent(new MouseEvent("click", { bubbles: true, button: 0 }));
    expect(send).not.toHaveBeenCalled();
  });

  it("listens in the capture phase so the page cannot stop it first", () => {
    const add = vi.fn();
    installLinkHandOff({ addEventListener: add }, () => {});
    expect(add).toHaveBeenCalledWith("click", expect.any(Function), true);
    const listener = add.mock.calls[0]?.[1] as (event: unknown) => void;
    const send = vi.fn();
    installLinkHandOff(
      { addEventListener: (_t, l) => l({ isTrusted: true, button: 0, target: el("blank") }) },
      send,
    );
    expect(send).toHaveBeenCalledWith(GUEST_LINK_CHANNEL, expect.stringContaining("/docs?x=1"));
    expect(typeof listener).toBe("function");
  });

  it("the bundled preload exposes nothing to the page", async () => {
    const before = new Set(Object.keys(window));
    await import("./guestPreloadEntry.ts");
    expect(electron.exposeInMainWorld).not.toHaveBeenCalled();
    expect(Object.keys(window).filter((key) => !before.has(key))).toEqual([]);
    (el("blank") as HTMLAnchorElement).click(); // untrusted
    expect(electron.sendToHost).not.toHaveBeenCalled();
  });
});
