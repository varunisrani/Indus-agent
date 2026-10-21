// @vitest-environment jsdom
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { GUEST_LINK_CHANNEL as MAIN_CHANNEL } from "../../../electron/preview/guestPreload.ts";
import { PREVIEW_PARTITION as MAIN_PARTITION } from "../../../electron/security/navigation.ts";
import { type PreviewBridge, PreviewPane } from "./PreviewPane.tsx";
import {
  fileUrlOf,
  fileViewOf,
  GUEST_LINK_CHANNEL,
  PREVIEW_PARTITION,
  parseAddress,
  pathFromFileUrl,
} from "./target.ts";

function fakeBridge(overrides: Partial<Record<string, unknown>> = {}) {
  const fileListeners = new Set<(change: { watchId: string; path: string }) => void>();
  const bridge = {
    fs: {
      readText: vi.fn(async () => ({ text: "hello", truncated: false, size: 5, binary: false })),
      readDataUrl: vi.fn(async () => ({
        dataUrl: "data:image/png;base64,AA",
        size: 1,
        mimeType: "image/png",
      })),
    },
    preview: {
      capture: vi.fn(async () => ({ dataUrl: "data:image/png;base64,CAP" })),
      watchFile: vi.fn(async () => ({ watchId: "w1" })),
      unwatchFile: vi.fn(async () => ({ ok: true as const })),
      onFileChanged: vi.fn((listener: (change: { watchId: string; path: string }) => void) => {
        fileListeners.add(listener);
        return () => fileListeners.delete(listener);
      }),
    },
    external: { open: vi.fn(async () => ({ ok: true as const })) },
    ...overrides,
  };
  const emitFileChanged = (change: { watchId: string; path: string }) => {
    for (const l of [...fileListeners]) l(change);
  };
  return { bridge: bridge as unknown as PreviewBridge, raw: bridge, emitFileChanged };
}

function webview(): HTMLElement & { getWebContentsId?: () => number } {
  const found = document.querySelector("webview");
  if (found === null) throw new Error("no webview");
  return found as HTMLElement;
}

function guestEvent(type: string, fields: Record<string, unknown>) {
  const event = new Event(type);
  Object.assign(event, fields);
  return event;
}

describe("target parsing", () => {
  it("accepts web addresses, dev servers and files; refuses other schemes", () => {
    expect(parseAddress("localhost:5173")).toEqual({ kind: "url", url: "http://localhost:5173/" });
    expect(parseAddress("127.0.0.1:8000/x")).toEqual({
      kind: "url",
      url: "http://127.0.0.1:8000/x",
    });
    expect(parseAddress("example.com/docs")).toEqual({
      kind: "url",
      url: "https://example.com/docs",
    });
    expect(parseAddress("https://example.com")).toEqual({
      kind: "url",
      url: "https://example.com/",
    });
    expect(parseAddress("/home/me/site/index.html")).toEqual({
      kind: "file",
      path: "/home/me/site/index.html",
    });
    expect(parseAddress("file:///home/me/a%20b.txt")).toEqual({
      kind: "file",
      path: "/home/me/a b.txt",
    });
    expect(parseAddress("C:\\site\\index.html")).toEqual({
      kind: "file",
      path: "C:\\site\\index.html",
    });
    for (const bad of [
      "javascript:alert(1)",
      "data:text/html,x",
      "indus://open/x",
      "ftp://x.y/",
      "",
      "word",
    ]) {
      expect(parseAddress(bad)).toBeNull();
    }
    expect(parseAddress("https://user:pw@example.com/")).toBeNull();
    expect(pathFromFileUrl("file://otherhost/share/x")).toBeNull();
    expect(pathFromFileUrl("file:///C:/site/a.html")).toBe("C:\\site\\a.html");
    expect(fileUrlOf("/home/me/a b.html")).toBe("file:///home/me/a%20b.html");
    expect(fileUrlOf("C:\\site\\a.html")).toBe("file:///C%3A/site/a.html");
    expect([fileViewOf("a.HTML"), fileViewOf("b.png"), fileViewOf("c.ts")]).toEqual([
      "html",
      "image",
      "text",
    ]);
  });

  it("the renderer's constants equal main's", () => {
    expect(PREVIEW_PARTITION).toBe(MAIN_PARTITION);
    expect(GUEST_LINK_CHANNEL).toBe(MAIN_CHANNEL);
  });
});

describe("PreviewPane", () => {
  it("renders the webview on the preview partition without popups", () => {
    const { bridge } = fakeBridge();
    render(
      <PreviewPane
        target={{ kind: "url", url: "http://localhost:3000/" }}
        mode="local"
        bridge={bridge}
      />,
    );
    const view = webview();
    expect(view.getAttribute("partition")).toBe("persist:indus-preview");
    expect(view.hasAttribute("allowpopups")).toBe(false);
    expect(view.getAttribute("webpreferences")).toBe(
      "contextIsolation=yes,sandbox=yes,nodeIntegration=no",
    );
    expect(view.getAttribute("src")).toBe("http://localhost:3000/");
  });

  it("a guest hand-off goes to external.open with the guest id", async () => {
    const { bridge, raw } = fakeBridge();
    render(
      <PreviewPane
        target={{ kind: "url", url: "http://localhost:3000/" }}
        mode="local"
        bridge={bridge}
      />,
    );
    const view = webview();
    view.getWebContentsId = () => 77;
    act(() => {
      view.dispatchEvent(
        guestEvent("ipc-message", {
          channel: GUEST_LINK_CHANNEL,
          args: ["https://docs.example/a"],
        }),
      );
      view.dispatchEvent(
        guestEvent("ipc-message", { channel: "something-else", args: ["https://x.example/"] }),
      );
    });
    await waitFor(() => expect(raw.external.open).toHaveBeenCalledTimes(1));
    expect(raw.external.open).toHaveBeenCalledWith({
      url: "https://docs.example/a",
      guestContentsId: 77,
    });
  });

  it("a file: hand-off from the guest is refused before main", async () => {
    const { bridge, raw } = fakeBridge();
    render(
      <PreviewPane
        target={{ kind: "url", url: "http://localhost:3000/" }}
        mode="local"
        bridge={bridge}
      />,
    );
    const view = webview();
    view.getWebContentsId = () => 77;
    act(() => {
      view.dispatchEvent(
        guestEvent("ipc-message", { channel: GUEST_LINK_CHANNEL, args: ["file:///etc/passwd"] }),
      );
      view.dispatchEvent(
        guestEvent("ipc-message", { channel: GUEST_LINK_CHANNEL, args: ["javascript:alert(1)"] }),
      );
    });
    expect((await screen.findByRole("status")).textContent).toMatch(/only web links/i);
    expect(raw.external.open).not.toHaveBeenCalled();
  });

  it("our own Open in browser button asks main with the current page", async () => {
    const { bridge, raw } = fakeBridge();
    render(
      <PreviewPane
        target={{ kind: "url", url: "http://localhost:3000/" }}
        mode="local"
        bridge={bridge}
      />,
    );
    act(() => {
      webview().dispatchEvent(guestEvent("did-navigate", { url: "http://localhost:3000/next" }));
    });
    fireEvent.click(screen.getByRole("button", { name: "Open in browser" }));
    await waitFor(() =>
      expect(raw.external.open).toHaveBeenCalledWith({ url: "http://localhost:3000/next" }),
    );
  });

  it("a sub-frame load failure does not show the error; a main-frame one does", async () => {
    const { bridge } = fakeBridge();
    render(
      <PreviewPane
        target={{ kind: "url", url: "http://localhost:3000/" }}
        mode="local"
        bridge={bridge}
      />,
    );
    act(() => {
      webview().dispatchEvent(
        guestEvent("did-fail-load", {
          isMainFrame: false,
          errorCode: -105,
          errorDescription: "ad blocked",
        }),
      );
      webview().dispatchEvent(
        guestEvent("did-fail-load", {
          isMainFrame: true,
          errorCode: -3,
          errorDescription: "aborted",
        }),
      );
    });
    expect(screen.queryByRole("alert")).toBeNull();
    act(() => {
      webview().dispatchEvent(
        guestEvent("did-fail-load", {
          isMainFrame: true,
          errorCode: -102,
          errorDescription: "ERR_CONNECTION_REFUSED",
        }),
      );
    });
    expect((await screen.findByRole("alert")).textContent).toMatch("ERR_CONNECTION_REFUSED");
  });

  it("capture: an image of the guest, offered to the chat", async () => {
    const { bridge, raw } = fakeBridge();
    const attach = vi.fn();
    render(
      <PreviewPane
        target={{ kind: "url", url: "http://localhost:3000/" }}
        mode="local"
        bridge={bridge}
        onAttachCapture={attach}
      />,
    );
    webview().getWebContentsId = () => 9;
    fireEvent.click(screen.getByRole("button", { name: "Capture as image" }));
    await screen.findByAltText("Captured image of the page");
    expect(raw.preview.capture).toHaveBeenCalledWith({ guestContentsId: 9 });
    fireEvent.click(screen.getByRole("button", { name: "Add to chat" }));
    expect(attach).toHaveBeenCalledWith("data:image/png;base64,CAP");
  });

  it("a 600 KiB text file shows the truncation notice", async () => {
    const { bridge, raw } = fakeBridge();
    raw.fs.readText.mockResolvedValueOnce({
      text: "x".repeat(512 * 1024),
      truncated: true,
      size: 600 * 1024,
      binary: false,
    });
    render(
      <PreviewPane
        target={{ kind: "file", path: "/home/me/big.log" }}
        mode="local"
        bridge={bridge}
      />,
    );
    expect(await screen.findByText("Showing the first 512 KiB of this file.")).toBeTruthy();
    expect(document.querySelector("webview")).toBeNull();
  });

  it("live reload re-reads the file on main's (debounced) change event; unwatches on close", async () => {
    const { bridge, raw, emitFileChanged } = fakeBridge();
    const view = render(
      <PreviewPane
        target={{ kind: "file", path: "/home/me/notes.md" }}
        mode="local"
        bridge={bridge}
      />,
    );
    await screen.findByText("hello");
    await waitFor(() =>
      expect(raw.preview.watchFile).toHaveBeenCalledWith({ path: "/home/me/notes.md" }),
    );
    raw.fs.readText.mockResolvedValueOnce({
      text: "hello again",
      truncated: false,
      size: 11,
      binary: false,
    });
    act(() => emitFileChanged({ watchId: "other", path: "/x" }));
    expect(raw.fs.readText).toHaveBeenCalledTimes(1);
    act(() => emitFileChanged({ watchId: "w1", path: "/home/me/notes.md" }));
    await screen.findByText("hello again");
    view.unmount();
    expect(raw.preview.unwatchFile).toHaveBeenCalledWith({ watchId: "w1" });
  });

  it("an HTML file shows in the guest by its file URL; images as data URLs; remote files are not read", async () => {
    const { bridge, raw } = fakeBridge();
    const first = render(
      <PreviewPane
        target={{ kind: "file", path: "/home/me/site/index.html" }}
        mode="local"
        bridge={bridge}
      />,
    );
    expect(webview().getAttribute("src")).toBe("file:///home/me/site/index.html");
    first.unmount();
    const second = render(
      <PreviewPane
        target={{ kind: "file", path: "/home/me/pic.png" }}
        mode="local"
        bridge={bridge}
      />,
    );
    expect((await screen.findByRole("img")).getAttribute("src")).toBe("data:image/png;base64,AA");
    expect(raw.fs.readDataUrl).toHaveBeenCalledWith({
      path: "/home/me/pic.png",
      purpose: "preview",
    });
    second.unmount();
    render(
      <PreviewPane
        target={{ kind: "file", path: "/srv/remote.txt" }}
        mode="remote"
        bridge={bridge}
      />,
    );
    expect(screen.getByText(/previewed from the Files pane/)).toBeTruthy();
    expect(raw.fs.readText).not.toHaveBeenCalled();
  });

  it("the address bar refuses other schemes and navigates the same guest", async () => {
    const { bridge } = fakeBridge();
    const onNavigate = vi.fn();
    render(
      <PreviewPane
        target={{ kind: "url", url: "http://localhost:3000/" }}
        mode="local"
        bridge={bridge}
        onNavigate={onNavigate}
      />,
    );
    const view = webview() as HTMLElement & { loadURL?: (url: string) => Promise<void> };
    const loadURL = vi.fn(async () => {});
    view.loadURL = loadURL;
    const field = screen.getByRole("textbox", { name: "Address" });
    fireEvent.change(field, { target: { value: "javascript:alert(1)" } });
    fireEvent.submit(field);
    expect((await screen.findByRole("status")).textContent).toMatch(/Only web addresses/);
    fireEvent.change(field, { target: { value: "localhost:4000" } });
    fireEvent.submit(field);
    expect(loadURL).toHaveBeenCalledWith("http://localhost:4000/");
    expect(onNavigate).toHaveBeenCalledWith({ kind: "url", url: "http://localhost:4000/" });
    expect(webview()).toBe(view);
  });

  it("hidden keeps the guest mounted but inert", () => {
    const { bridge } = fakeBridge();
    const view = render(
      <PreviewPane
        target={{ kind: "url", url: "http://localhost:3000/" }}
        mode="local"
        bridge={bridge}
      />,
    );
    const guest = webview();
    view.rerender(
      <PreviewPane
        target={{ kind: "url", url: "http://localhost:3000/" }}
        mode="local"
        bridge={bridge}
        hidden
      />,
    );
    expect(webview()).toBe(guest);
    expect(screen.getByTestId("preview-pane").hasAttribute("inert")).toBe(true);
  });
});
