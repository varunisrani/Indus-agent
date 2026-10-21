// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  AnnotateSession,
  composeMessage,
  containerOf,
  GUEST_GLOBAL,
  type GuestApi,
  type GuestPick,
  groupPins,
  guestApi,
  guestCall,
  guestInstallScript,
  MARKUP_BUDGET,
  type Pin,
} from "./annotate.ts";

const SECRET = ["hunter", "2", "-correct-horse"].join("");
const TOKEN = ["tok", "_", "a1b2c3d4e5"].join("");

function page(): void {
  document.body.innerHTML = `
    <main class="app">
      <section class="login">
        <form id="signin">
          <input name="email" value="me@example.com">
          <input type="password" name="pw">
          <input type="hidden" name="csrf" value="${TOKEN}">
          <textarea name="api_token">${TOKEN}</textarea>
          <button data-auth-token="${TOKEN}" class="primary">Sign in</button>
          <span data-secret>${SECRET}</span>
        </form>
      </section>
      <section class="pricing">
        <h2>Plans</h2>
        <p class="lede">Pick one</p>
      </section>
    </main>`;
  (document.querySelector('input[type="password"]') as HTMLInputElement).value = SECRET;
}

afterEach(() => {
  document.body.innerHTML = "";
  delete (window as unknown as Record<string, unknown>)[GUEST_GLOBAL];
});

function pin(number: number, selector: string, note = `note ${number}`): Pin {
  return { number, selector, note, html: "", text: "", rect: null };
}

describe("redaction in the guest", () => {
  it("no password, hidden value or secret attribute leaves the guest", () => {
    page();
    const api = guestApi(window);
    const form = api.pick("form#signin") as GuestPick;
    const button = api.pick("button.primary") as GuestPick;
    const all = JSON.stringify([form, button]);
    expect(all).not.toContain(SECRET);
    expect(all).not.toContain(TOKEN);
    expect(form.html).toContain("[redacted]");
    expect(form.html).toContain("me@example.com"); // a plain field keeps its value
    // the live page is untouched
    expect((document.querySelector('input[type="password"]') as HTMLInputElement).value).toBe(
      SECRET,
    );
    expect(document.querySelector("button")?.getAttribute("data-auth-token")).toBe(TOKEN);
  });

  it("the outgoing annotation never carries the password", () => {
    page();
    const api = guestApi(window);
    const sent: string[] = [];
    const session = new AnnotateSession({ onSend: (m) => sent.push(m) });
    const added = session.add(api.pick("form#signin") as GuestPick) as Pin;
    session.setNote(added.number, "the button is too small");
    session.send("http://localhost:5173/");
    expect(sent).toHaveLength(1);
    expect(sent[0]).not.toContain(SECRET);
    expect(sent[0]).not.toContain(TOKEN);
    expect(sent[0]).toContain("the button is too small");
  });

  it("markup is capped and selectors are short paths", () => {
    document.body.innerHTML = `<div class="wrap"><p class="long">${"x".repeat(2_000)}</p><p>b</p></div>`;
    const api = guestApi(window);
    const pick = api.pick("p.long") as GuestPick;
    expect(pick.html.length).toBeLessThanOrEqual(MARKUP_BUDGET + 1);
    expect(pick.selector).toBe("body>div.wrap>p.long:nth-of-type(1)");
    expect(document.querySelector(pick.selector)).toBe(document.querySelector("p.long"));
  });

  it("the install script is self-contained and idempotent", () => {
    page();
    // biome-ignore lint/security/noGlobalEval: the guest script runs in this test page, as executeJavaScript would run it in the webview
    const run = (code: string) => window.eval(code) as unknown;
    expect(run(guestInstallScript())).toBe(true);
    const api = (window as unknown as Record<string, GuestApi>)[GUEST_GLOBAL] as GuestApi;
    expect(run(guestInstallScript())).toBe(true);
    expect((window as unknown as Record<string, GuestApi>)[GUEST_GLOBAL]).toBe(api);
    const picked = run(guestCall("pick", "h2")) as GuestPick;
    expect(picked.text).toBe("Plans");
    expect(guestCall("act", { action: "click", selector: 'a");alert(1);("' })).toContain(
      '\\");alert(1);(\\"',
    );
  });

  it("clicks in annotate mode become picks, and stop ends the wait", async () => {
    page();
    const api = guestApi(window);
    api.start();
    const waiting = api.next();
    (document.querySelector("h2") as HTMLElement).click();
    const first = (await waiting) as { type: string; pick: GuestPick };
    expect(first.type).toBe("pick");
    expect(first.pick.text).toBe("Plans");
    api.stop();
    expect(await api.next()).toEqual({ type: "end" });
  });
});

describe("batching by subtree", () => {
  it("two pins in one subtree make one batch", () => {
    const batches = groupPins([
      pin(1, "body>main.app>section.pricing>h2"),
      pin(2, "body>main.app>section.pricing>p.lede"),
    ]);
    expect(batches).toHaveLength(1);
    expect(batches[0]?.label).toBe("section.pricing");
    expect(batches[0]?.pins.map((p) => p.number)).toEqual([1, 2]);
  });

  it("disjoint subtrees stay apart, nested pins join their container, loose pins trail", () => {
    const batches = groupPins([
      pin(1, "body>main.app>section.login>form#signin>button.primary"),
      pin(2, "body>main.app>section.pricing>h2"),
      pin(3, ""),
      pin(4, "body>main.app>section.login"),
      pin(5, "body>main.app>section.login>form#signin>input"),
    ]);
    expect(batches.map((b) => b.pins.map((p) => p.number))).toEqual([[1, 4, 5], [2], [3]]);
    expect(batches[2]?.key).toBe("");
  });

  it("one pin is its own batch; container of a one-step selector is itself", () => {
    expect(groupPins([pin(7, "body>header")])).toEqual([
      { key: "body", label: "body", pins: [pin(7, "body>header")] },
    ]);
    expect(containerOf("body")).toBe("body");
  });

  it("the message lists every batch in pin order", () => {
    const text = composeMessage(
      "http://localhost:3000/",
      groupPins([pin(2, "a>b>c"), pin(1, "a>b>d")]),
    );
    expect(text).toContain("Notes on http://localhost:3000/");
    expect(text.indexOf("1. note 1")).toBeLessThan(text.indexOf("2. note 2"));
  });
});

describe("annotate never sends by itself", () => {
  it("leaving the mode, waiting or removing pins sends nothing", () => {
    vi.useFakeTimers();
    try {
      const onSend = vi.fn();
      const session = new AnnotateSession({ onSend });
      session.add({ selector: "body>h1", html: "<h1>Hi</h1>", text: "Hi", rect: null });
      session.add({ selector: "body>p", html: "<p>x</p>", text: "x", rect: null });
      vi.advanceTimersByTime(60 * 60 * 1000);
      session.remove(2);
      session.discard();
      expect(onSend).not.toHaveBeenCalled();
      expect(session.send("x")).toBe(false); // nothing left to send
      expect(onSend).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it("send sends one message once and clears the pins", () => {
    const onSend = vi.fn();
    const session = new AnnotateSession({ onSend });
    session.add({ selector: "body>h1", html: "", text: "", rect: null });
    expect(session.send("p")).toBe(true);
    expect(session.send("p")).toBe(false);
    expect(onSend).toHaveBeenCalledTimes(1);
    expect(session.list()).toEqual([]);
  });
});
