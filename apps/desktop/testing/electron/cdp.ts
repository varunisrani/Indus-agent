// A small Chrome DevTools Protocol client for driving the built app in tests: evaluate in the
// page, wait for a condition, type, press a key and click a button with real input events.

import fs from "node:fs";

interface Pending {
  resolve: (value: Record<string, unknown>) => void;
  reject: (error: Error) => void;
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/** The first app page (`file:` URL) a `--remote-debugging-port` Electron offers. */
export async function appPage(port: number, timeoutMs = 30_000): Promise<string> {
  const until = Date.now() + timeoutMs;
  while (Date.now() < until) {
    const list = (await fetch(`http://127.0.0.1:${port}/json/list`)
      .then((answer) => answer.json())
      .catch(() => [])) as Array<{ type: string; url: string; webSocketDebuggerUrl: string }>;
    const page = list.find((target) => target.type === "page" && target.url.startsWith("file:"));
    if (page !== undefined) return page.webSocketDebuggerUrl;
    await sleep(200);
  }
  throw new Error("the app opened no page");
}

export class Cdp {
  private readonly socket: WebSocket;
  private serial = 0;
  private readonly pending = new Map<number, Pending>();
  private closedBy: string | null = null;

  private constructor(socket: WebSocket) {
    this.socket = socket;
    // Chromium can drop a session it has just answered (seen on a reused <webview> guest right
    // after it navigates: the socket closes with 1006). Fail every waiting and later call at once
    // instead of letting each one run into its own timeout.
    socket.addEventListener("close", (event) => {
      this.closedBy = `devtools socket closed (${event.code})`;
      for (const waiter of this.pending.values()) waiter.reject(new Error(this.closedBy));
      this.pending.clear();
    });
    socket.onmessage = (message) => {
      const frame = JSON.parse(String(message.data)) as {
        id?: number;
        result?: Record<string, unknown>;
        error?: unknown;
      };
      const waiter = frame.id === undefined ? undefined : this.pending.get(frame.id);
      if (waiter === undefined || frame.id === undefined) return;
      this.pending.delete(frame.id);
      if (frame.error !== undefined) waiter.reject(new Error(JSON.stringify(frame.error)));
      else waiter.resolve(frame.result ?? {});
    };
  }

  static async open(url: string): Promise<Cdp> {
    const socket = new WebSocket(url);
    await new Promise<void>((resolve, reject) => {
      socket.onopen = () => resolve();
      socket.onerror = () => reject(new Error("devtools socket failed"));
    });
    return new Cdp(socket);
  }

  /** True once the DevTools socket is gone (closed by us or by the browser). */
  get closed(): boolean {
    return this.closedBy !== null;
  }

  /**
   * One DevTools call; a target that went away (navigated, crashed) fails it after `timeoutMs`,
   * a closed session at once.
   */
  send(
    method: string,
    params: Record<string, unknown> = {},
    timeoutMs = 30_000,
  ): Promise<Record<string, unknown>> {
    if (this.closedBy !== null) return Promise.reject(new Error(this.closedBy));
    this.serial += 1;
    const id = this.serial;
    this.socket.send(JSON.stringify({ id, method, params }));
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`devtools ${method} got no answer in ${timeoutMs} ms`));
      }, timeoutMs);
      this.pending.set(id, {
        resolve: (value) => {
          clearTimeout(timer);
          resolve(value);
        },
        reject: (error) => {
          clearTimeout(timer);
          reject(error);
        },
      });
    });
  }

  async eval<T = unknown>(expression: string): Promise<T> {
    const answer = (await this.send("Runtime.evaluate", {
      expression,
      awaitPromise: true,
      returnByValue: true,
    })) as { result?: { value?: T }; exceptionDetails?: unknown };
    if (answer.exceptionDetails !== undefined) {
      throw new Error(`page threw: ${JSON.stringify(answer.exceptionDetails).slice(0, 300)}`);
    }
    return answer.result?.value as T;
  }

  /** `eval` that gives up after `timeoutMs` (a page between documents may not answer). */
  async evalWithin<T = unknown>(expression: string, timeoutMs: number): Promise<T> {
    const answer = (await this.send(
      "Runtime.evaluate",
      { expression, awaitPromise: true, returnByValue: true },
      timeoutMs,
    )) as { result?: { value?: T }; exceptionDetails?: unknown };
    if (answer.exceptionDetails !== undefined) {
      throw new Error(`page threw: ${JSON.stringify(answer.exceptionDetails).slice(0, 300)}`);
    }
    return answer.result?.value as T;
  }

  /** Poll `expression` until truthy; on timeout the error carries the page's text. */
  async waitFor(expression: string, what: string, timeoutMs = 30_000): Promise<void> {
    const until = Date.now() + timeoutMs;
    for (;;) {
      if (await this.evalWithin<boolean>(expression, 5_000).catch(() => false)) return;
      if (this.closedBy !== null) throw new Error(`${this.closedBy} while waiting for ${what}`);
      if (Date.now() > until) {
        const text = await this.eval<string>("document.body.innerText").catch(() => "?");
        throw new Error(`timed out waiting for ${what}; the page shows:\n${text}`);
      }
      await sleep(100);
    }
  }

  async type(selector: string, text: string): Promise<void> {
    await this.eval(`document.querySelector(${JSON.stringify(selector)}).focus()`);
    await this.send("Input.insertText", { text });
  }

  async pressEnter(): Promise<void> {
    const key = { key: "Enter", code: "Enter", windowsVirtualKeyCode: 13 };
    await this.send("Input.dispatchKeyEvent", { type: "rawKeyDown", ...key });
    await this.send("Input.dispatchKeyEvent", { type: "char", key: "Enter", text: "\r" });
    await this.send("Input.dispatchKeyEvent", { type: "keyUp", ...key });
  }

  /** A real click on the button labelled `label` (its text, or an icon button's `aria-label`). */
  async click(label: string): Promise<void> {
    const at = await this.eval<{ x: number; y: number } | null>(`(() => {
      const button = [...document.querySelectorAll("button")]
        .find((b) => b.textContent.trim() === ${JSON.stringify(label)} || b.getAttribute("aria-label") === ${JSON.stringify(label)});
      if (!button) return null;
      button.scrollIntoView({ block: "center" });
      const r = button.getBoundingClientRect();
      return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
    })()`);
    if (at === null) throw new Error(`no button "${label}"`);
    for (const type of ["mouseMoved", "mousePressed", "mouseReleased"]) {
      await this.send("Input.dispatchMouseEvent", { type, ...at, button: "left", clickCount: 1 });
    }
  }

  async screenshot(file: string): Promise<void> {
    const shot = (await this.send("Page.captureScreenshot", { format: "png" }).catch(
      () => null,
    )) as { data?: string } | null;
    if (shot?.data !== undefined) fs.writeFileSync(file, Buffer.from(shot.data, "base64"));
  }

  close(): void {
    this.socket.close();
  }
}

/** Text of the transcript area. */
export const TRANSCRIPT_TEXT = `document.querySelector('.transcript')?.innerText ?? ''`;

/** True while a button labelled `label` (its text, or an icon button's `aria-label`) is on screen. */
export function hasButton(label: string): string {
  const quoted = JSON.stringify(label);
  return `[...document.querySelectorAll('button')].some((b) => b.textContent.trim() === ${quoted} || b.getAttribute('aria-label') === ${quoted})`;
}
