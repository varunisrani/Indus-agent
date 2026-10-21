// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { GUEST_GLOBAL } from "./annotate.ts";
import { PreviewAutomation, type PreviewGuestHandle, type PreviewRequest } from "./automation.ts";

const SECRET = ["s3cr", "et-", "value"].join("");

/** A guest that runs scripts against this jsdom page (standing in for the webview's page). */
function guest(sessionId: string | null) {
  const scripts: string[] = [];
  const loaded: string[] = [];
  const handle: PreviewGuestHandle = {
    executeJavaScript: vi.fn(async (code: string) => {
      scripts.push(code);
      // biome-ignore lint/security/noGlobalEval: the guest script runs in this test page, as executeJavaScript would run it in the webview
      return window.eval(code) as unknown;
    }),
    loadURL: vi.fn(async (url: string) => {
      loaded.push(url);
    }),
    sessionId: () => sessionId,
  };
  return { handle, scripts, loaded };
}

function req(fields: Partial<PreviewRequest>): PreviewRequest {
  return { kind: "preview.act", request_id: "srq-1", session_id: "s-1", ...fields };
}

afterEach(() => {
  document.body.innerHTML = "";
  delete (window as unknown as Record<string, unknown>)[GUEST_GLOBAL];
});

describe("preview automation", () => {
  it("a request for a session this window does not show is declined not_shown", async () => {
    const host = new PreviewAutomation();
    expect(await host.handle(req({ action: "click", selector: "a" }))).toEqual({
      declined: "not_shown",
    });
    const { handle, scripts } = guest("s-2");
    host.attach(handle);
    expect(await host.handle(req({ action: "click", selector: "a" }))).toEqual({
      declined: "not_shown",
    });
    expect(await host.handle(req({ kind: "preview.read", what: "snapshot" }))).toEqual({
      declined: "not_shown",
    });
    expect(scripts).toEqual([]);
    const unset = guest(null);
    host.attach(unset.handle);
    expect(await host.handle(req({ action: "click" }))).toEqual({ declined: "not_shown" });
  });

  it("acts inside the guest page with JSON arguments", async () => {
    document.body.innerHTML = `<form><input id="q" name="q"><button id="go" type="button">Go</button></form>`;
    const clicked = vi.fn();
    document.getElementById("go")?.addEventListener("click", clicked);
    const host = new PreviewAutomation();
    const { handle, scripts } = guest("s-1");
    host.attach(handle);
    expect(
      await host.handle(req({ action: "type", selector: "#q", text: 'tea "and" cake' })),
    ).toEqual({ ok: true });
    expect((document.getElementById("q") as HTMLInputElement).value).toBe('tea "and" cake');
    expect(await host.handle(req({ action: "click", selector: "#go" }))).toMatchObject({
      ok: true,
    });
    expect(clicked).toHaveBeenCalledTimes(1);
    expect(await host.handle(req({ action: "click", selector: "#missing" }))).toEqual({
      ok: false,
      error: "no element matches #missing",
    });
    // every call carries its arguments as JSON after the install script
    expect(scripts.filter((s) => s.includes("act(")).every((s) => s.includes('{"action":'))).toBe(
      true,
    );
  });

  it("snapshot reads the page with secrets redacted", async () => {
    document.title = "Shop";
    document.body.innerHTML = `<h1>Basket</h1><input type="password" name="pw"><input name="coupon"><textarea name="session_key">${SECRET}</textarea>`;
    (document.querySelector('input[name="pw"]') as HTMLInputElement).value = SECRET;
    (document.querySelector('input[name="coupon"]') as HTMLInputElement).value = "SPRING";
    const host = new PreviewAutomation();
    host.attach(guest("s-1").handle);
    const snap = (await host.handle(req({ kind: "preview.read", what: "snapshot" }))) as Record<
      string,
      unknown
    >;
    expect(snap).toMatchObject({ ok: true, title: "Shop" });
    expect(JSON.stringify(snap)).not.toContain(SECRET);
    expect(JSON.stringify(snap)).toContain("SPRING");
    expect(snap.text).toContain("Basket");
  });

  it("typing into a secret field is left to the person", async () => {
    document.body.innerHTML = `<input type="password" id="pw">`;
    const host = new PreviewAutomation();
    host.attach(guest("s-1").handle);
    expect(await host.handle(req({ action: "type", selector: "#pw", text: "x" }))).toEqual({
      ok: false,
      error: "secret fields are left to the person",
    });
  });

  it("navigates inside the guest, web and local files only", async () => {
    const host = new PreviewAutomation();
    const { handle, loaded } = guest("s-1");
    host.attach(handle);
    expect(await host.handle(req({ action: "navigate", url: "http://localhost:5173/x" }))).toEqual({
      ok: true,
      url: "http://localhost:5173/x",
    });
    expect(await host.handle(req({ action: "navigate", url: "file:///tmp/a.html" }))).toMatchObject(
      {
        ok: true,
      },
    );
    for (const url of [
      "javascript:alert(1)",
      "data:text/html,x",
      "indus://open",
      "https://u:p@h/",
    ]) {
      expect(await host.handle(req({ action: "navigate", url }))).toMatchObject({ ok: false });
    }
    expect(loaded).toEqual(["http://localhost:5173/x", "file:///tmp/a.html"]);
  });

  it("agent pins are drawn on the page; unknown actions and detached guests are refused", async () => {
    document.body.innerHTML = `<h1>Title</h1>`;
    const host = new PreviewAutomation();
    const { handle } = guest("s-1");
    const detach = host.attach(handle);
    const answer = await host.handle(
      req({
        action: "annotate",
        pins: [
          { selector: "h1", note: "typo" },
          { selector: "#gone", note: "x" },
        ],
      }),
    );
    expect(answer).toEqual({ ok: true, shown: 1, missing: 1 });
    expect(document.querySelector('[data-indus-pin="1"]')?.textContent).toBe("1");
    expect(await host.handle(req({ action: "explode" }))).toEqual({
      ok: false,
      error: "unknown action explode",
    });
    detach();
    expect(host.shownSession()).toBeNull();
    expect(await host.handle(req({ action: "click", selector: "h1" }))).toEqual({
      declined: "not_shown",
    });
  });

  it("a page that never answers times out", async () => {
    const host = new PreviewAutomation({ timeoutMs: 20 });
    host.attach({
      executeJavaScript: () => new Promise(() => {}),
      loadURL: async () => {},
      sessionId: () => "s-1",
    });
    expect(await host.handle(req({ action: "click", selector: "a" }))).toEqual({
      ok: false,
      error: "no answer from the page within 20 ms",
    });
  });
});
