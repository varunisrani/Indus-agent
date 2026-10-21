import { describe, expect, it } from "vitest";
import { FakeTimers } from "../../testing/fakeTimers.ts";
import {
  checkLinkUrl,
  decodeEntities,
  extractIconHref,
  extractTitle,
  FAVICON_MAX_BYTES,
  LINK_PARTITION,
  LINK_WINDOW_TIMEOUT_MS,
  LinkPreviewer,
  type LinkWindowLike,
  linkWindowOptions,
} from "./linkPreview.ts";

describe("link URLs", () => {
  it.each([
    "file:///etc/passwd",
    "javascript:alert(1)",
    "indus://open/x",
    "https://user:secret@example.com/",
    "http://localhost:8080/",
    "http://127.0.0.1/",
    "http://10.1.2.3/",
    "http://192.168.0.1/",
    "http://172.20.0.1/",
    "http://169.254.169.254/latest",
    "http://[::1]/",
    "http://[fd00::1]/",
    "http://printer.local/",
    `https://example.com/${"a".repeat(3000)}`,
    "not a url",
  ])("refuses %s", (url) => {
    expect(checkLinkUrl(url)).toBeNull();
  });

  it("allows public http/https", () => {
    expect(checkLinkUrl("https://example.com/a?b=c")?.hostname).toBe("example.com");
    expect(checkLinkUrl("http://172.32.0.1/")).not.toBeNull();
  });
});

describe("HTML reading", () => {
  it("title, og:title fallback, entities, whitespace", () => {
    expect(extractTitle("<html><title>\n  A &amp; B &#x2014; C&#39;s  </title>")).toBe(
      "A & B — C's",
    );
    expect(extractTitle('<meta property="og:title" content="From OG">')).toBe("From OG");
    expect(extractTitle("<title> </title>")).toBeNull();
    expect(extractTitle(`<title>${"x".repeat(400)}</title>`)?.length).toBe(300);
    expect(decodeEntities("&bogus; &#0;")).toBe("&bogus; &#0;");
  });

  it("icon link resolved against the page, else /favicon.ico", () => {
    const base = new URL("https://example.com/docs/page");
    expect(extractIconHref('<link rel="shortcut icon" href="/static/i.png">', base)).toBe(
      "https://example.com/static/i.png",
    );
    expect(extractIconHref("<link rel=apple-touch-icon href=t.png>", base)).toBe(
      "https://example.com/docs/t.png",
    );
    expect(extractIconHref("<p>none</p>", base)).toBe("https://example.com/favicon.ico");
  });
});

class FakeLinkWindow implements LinkWindowLike {
  destroyed = false;
  title = "";
  loaded: string[] = [];
  muted = false;
  denyHandler: (() => { action: "deny" }) | null = null;
  readonly handlers = new Map<string, (...args: unknown[]) => void>();
  readonly webContents = {
    setAudioMuted: (muted: boolean) => {
      this.muted = muted;
    },
    getTitle: () => this.title,
    on: (event: string, listener: (...args: never[]) => void) => {
      this.handlers.set(event, listener as (...args: unknown[]) => void);
      return undefined;
    },
    setWindowOpenHandler: (handler: () => { action: "deny" }) => {
      this.denyHandler = handler;
    },
  };
  loadURL(url: string): Promise<unknown> {
    this.loaded.push(url);
    return Promise.resolve();
  }
  destroy(): void {
    this.destroyed = true;
  }
  isDestroyed(): boolean {
    return this.destroyed;
  }
}

type Route = (url: string) => Response;

function previewer(route: Route) {
  const timers = new FakeTimers();
  const windows: FakeLinkWindow[] = [];
  const options: Array<Record<string, unknown>> = [];
  const fetched: string[] = [];
  const links = new LinkPreviewer({
    fetch: async (url) => {
      fetched.push(url);
      return route(url);
    },
    createWindow: (opts) => {
      options.push(opts);
      const win = new FakeLinkWindow();
      windows.push(win);
      return win;
    },
    timers,
    now: () => timers.now,
    log: () => {},
  });
  return { links, timers, windows, options, fetched };
}

const PNG = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 1, 2, 3]);

async function settle(): Promise<void> {
  for (let i = 0; i < 20; i += 1) await new Promise((resolve) => setImmediate(resolve));
}

describe("link previews", () => {
  it("title and favicon from a plain fetch; cached afterwards", async () => {
    const { links, fetched, windows } = previewer((url) =>
      url.endsWith(".png")
        ? new Response(PNG, { headers: { "content-type": "image/png" } })
        : new Response('<title>Hello</title><link rel="icon" href="/i.png">', {
            headers: { "content-type": "text/html; charset=utf-8" },
          }),
    );
    const result = await links.preview("https://example.com/");
    expect(result.title).toBe("Hello");
    expect(result.favicon).toBe(`data:image/png;base64,${Buffer.from(PNG).toString("base64")}`);
    expect(windows).toHaveLength(0);
    await links.preview("https://example.com/");
    expect(fetched).toHaveLength(2);
  });

  it("refused URLs never fetch", async () => {
    const { links, fetched } = previewer(() => new Response(""));
    expect(await links.preview("file:///etc/passwd")).toEqual({ title: null, favicon: null });
    expect(await links.preview("http://127.0.0.1:9000/api")).toEqual({
      title: null,
      favicon: null,
    });
    expect(fetched).toEqual([]);
  });

  it("a favicon that is not an image or too big is dropped", async () => {
    const big = new Uint8Array(FAVICON_MAX_BYTES + 10);
    const { links } = previewer((url) =>
      url.endsWith(".ico")
        ? new Response(big, { headers: { "content-type": "image/x-icon" } })
        : url.endsWith(".svg")
          ? new Response("<svg/>", { headers: { "content-type": "text/html" } })
          : new Response('<title>T</title><link rel="icon" href="/x.svg">', {
              headers: { "content-type": "text/html" },
            }),
    );
    expect((await links.preview("https://a.example/")).favicon).toBeNull();
  });

  it("no title in the HTML: a hidden hardened window reads it, then is destroyed", async () => {
    const { links, windows, options } = previewer(
      () => new Response("<div id=app></div>", { headers: { "content-type": "text/html" } }),
    );
    const pending = links.preview("https://spa.example/");
    await settle();
    expect(windows).toHaveLength(1);
    const win = windows[0] as FakeLinkWindow;
    expect(win.loaded).toEqual(["https://spa.example/"]);
    expect(win.muted).toBe(true);
    expect(win.denyHandler?.()).toEqual({ action: "deny" });
    const prefs = options[0]?.webPreferences as Record<string, unknown>;
    expect(prefs).not.toHaveProperty("preload");
    expect(prefs).toMatchObject({
      sandbox: true,
      contextIsolation: true,
      nodeIntegration: false,
      partition: LINK_PARTITION,
    });
    expect(options[0]).toMatchObject({ show: false, focusable: false, skipTaskbar: true });
    win.title = "Rendered title";
    win.handlers.get("did-finish-load")?.();
    expect(await pending).toEqual({ title: "Rendered title", favicon: null });
    expect(win.destroyed).toBe(true);
  });

  it("the hidden window is destroyed after 8 s even if the page never finishes", async () => {
    const { links, windows, timers } = previewer(() => new Response("", { status: 500 }));
    const pending = links.preview("https://slow.example/");
    await settle();
    timers.advance(LINK_WINDOW_TIMEOUT_MS);
    expect(await pending).toEqual({ title: null, favicon: null });
    expect(windows[0]?.destroyed).toBe(true);
  });

  it("a page whose title is its URL has no title", async () => {
    expect(linkWindowOptions()).not.toHaveProperty("webPreferences.preload");
    const { links, windows } = previewer(() => new Response("x", { status: 404 }));
    const pending = links.preview("https://bare.example/");
    await settle();
    const win = windows[0] as FakeLinkWindow;
    win.title = "https://bare.example/";
    win.handlers.get("did-finish-load")?.();
    expect((await pending).title).toBeNull();
  });
});
