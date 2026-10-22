// The preview kinds through the window's connection (P24-25 wiring, D-16.799): the daemon's
// `preview.act` / `preview.read` server requests reach the window's preview automation; the window
// that shows the session's preview answers with what the page reported, any other declines.
import type { PreviewActRequest, PreviewReadRequest } from "@indus/protocol";
import { afterEach, describe, expect, it, vi } from "vitest";
import { type PreviewGuestHandle, windowPreviewAutomation } from "../panes/preview/automation.ts";
import { PREVIEW_KINDS, previewAnswer, ServerRequestStore } from "./serverRequests.ts";
import { harness, settle } from "./testSupport.ts";

function offer(request: PreviewActRequest | PreviewReadRequest) {
  return {
    jsonrpc: "2.0",
    method: "server_request",
    params: { request, replayed: false, generation: 1 },
  };
}

async function open(capabilities: string[]) {
  const h = harness();
  h.server.handle("server_request.answer", () => ({ accepted: true }));
  h.server.handle("server_request.decline", () => ({ accepted: true }));
  h.connection.start();
  await settle();
  h.ready(capabilities);
  await settle();
  await h.connection.watchSession("s1");
  return h;
}

let detach: (() => void) | null = null;

function showPreviewOf(sessionId: string) {
  const loaded: string[] = [];
  const guest: PreviewGuestHandle = {
    executeJavaScript: vi.fn(async () => ({ title: "Shop", address: "http://localhost:5173/" })),
    loadURL: vi.fn(async (url: string) => {
      loaded.push(url);
    }),
    sessionId: () => sessionId,
  };
  detach = windowPreviewAutomation.attach(guest);
  return { guest, loaded };
}

afterEach(() => {
  detach?.();
  detach = null;
});

describe("preview requests through the window's connection", () => {
  it("the window showing the session's preview answers with what the page did", async () => {
    const { loaded } = showPreviewOf("s1");
    const h = await open(["request.decline"]);
    h.server.sendRaw(
      offer({
        kind: "preview.act",
        request_id: "srq-p1",
        session_id: "s1",
        action: "navigate",
        url: "http://localhost:5173/",
        selector: null,
      }),
    );
    h.server.sendRaw(
      offer({ kind: "preview.read", request_id: "srq-p2", session_id: "s1", what: "snapshot" }),
    );
    await settle();
    await settle();
    expect(loaded).toEqual(["http://localhost:5173/"]);
    const answers = h.server.calls("server_request.answer").map((call) => call.params);
    expect(answers).toEqual([
      expect.objectContaining({
        request_id: "srq-p1",
        answer: {
          kind: "preview.act",
          ok: true,
          error: null,
          result: { url: "http://localhost:5173/" },
        },
      }),
      expect.objectContaining({
        request_id: "srq-p2",
        answer: {
          kind: "preview.read",
          ok: true,
          error: null,
          result: { title: "Shop", address: "http://localhost:5173/" },
        },
      }),
    ]);
    // never a card: the person is not asked
    expect(h.connection.serverRequests.list()).toEqual([]);
  });

  it("another session's request is declined not shown, never answered", async () => {
    showPreviewOf("s2");
    const h = await open(["request.decline"]);
    h.server.sendRaw(
      offer({ kind: "preview.read", request_id: "srq-p3", session_id: "s1", what: "snapshot" }),
    );
    await settle();
    await settle();
    expect(h.server.calls("server_request.decline").map((call) => call.params)).toEqual([
      expect.objectContaining({ request_id: "srq-p3" }),
    ]);
    expect(h.server.calls("server_request.answer")).toHaveLength(0);
  });

  it("a store without the window's automation leaves the preview kinds unhandled", () => {
    expect(Object.keys(new ServerRequestStore().handlers())).not.toContain("preview.act");
    const store = new ServerRequestStore({ preview: windowPreviewAutomation });
    expect(Object.keys(store.handlers())).toEqual(expect.arrayContaining([...PREVIEW_KINDS]));
  });

  it("maps the page's report to the protocol answer", () => {
    expect(previewAnswer("preview.act", { ok: false, error: "no such element" })).toEqual({
      kind: "preview.act",
      ok: false,
      error: "no such element",
      result: {},
    });
  });
});
