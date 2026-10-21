import { describe, expect, it } from "vitest";
import { ContributionRegistry } from "../contrib/registry.ts";
import { BotsSurface, botsContribution, registryHost } from "./BotsPage.tsx";
import { CANONICAL_TITLE, CanonicalChats, type RpcLike } from "./canonicalChat.ts";

/** A daemon with one bot whose forever-chat may get compressed. */
function daemon() {
  let tip: string | null = null;
  let mints = 0;
  const calls: string[] = [];
  let release: (() => void) | null = null;
  const rpc: RpcLike = {
    request: async (method, params) => {
      calls.push(`${method} ${JSON.stringify(params)}`);
      if (method !== "bots.open") throw new Error("unexpected");
      if (release === null) await new Promise<void>((resolve) => setTimeout(resolve, 5));
      if (tip === null) {
        mints += 1;
        tip = "chat-root";
        return { session_id: tip, created: true };
      }
      return { session_id: tip, created: false };
    },
  };
  return {
    rpc,
    calls,
    mints: () => mints,
    compress: () => {
      tip = "chat-tip-2";
    },
    hold: (fn: () => void) => {
      release = fn;
    },
  };
}

describe("canonical bot chat", () => {
  it("two concurrent clicks create one chat", async () => {
    const d = daemon();
    const chats = new CanonicalChats(d.rpc);
    const [a, b] = await Promise.all([chats.open("coder"), chats.open("coder")]);
    expect(a).toBe("chat-root");
    expect(b).toBe(a);
    expect(d.calls).toEqual(['bots.open {"profile":"coder"}']); // one request in flight
    expect(d.mints()).toBe(1);
    expect(await chats.open("coder")).toBe("chat-root"); // a later open asks again: no pointer
    expect(d.calls).toHaveLength(2);
  });

  it("canonical lookup after compression opens the live tip", async () => {
    const d = daemon();
    const chats = new CanonicalChats(d.rpc);
    chats.remember("coder", { session_id: "chat-root" }); // what the list said earlier
    await chats.open("coder");
    d.compress();
    expect(await chats.open("coder")).toBe("chat-tip-2");
    expect(chats.isCanonical("chat-root")).toBe(true);
    expect(chats.isCanonical("chat-tip-2")).toBe(true);
    expect(chats.ownerOf("chat-tip-2")).toBe("coder");
    expect(chats.isCanonical("side-chat")).toBe(false);
  });

  it("the canonical chat is absent from the sessions list; side-chats stay", () => {
    const chats = new CanonicalChats(daemon().rpc);
    chats.remember("coder", { session_id: "chat-tip-2" });
    const rows = [
      { id: "chat-tip-2", title: CANONICAL_TITLE, source: "bots" },
      { id: "x", lineage_root: "chat-tip-2", title: "segment" },
      { id: "other-bot", title: CANONICAL_TITLE, source: "bots" }, // never listed to us, still hidden
      { id: "side-chat", title: "Ask coder about the build", source: "bots" },
      { id: "plain", title: CANONICAL_TITLE, source: "cli" }, // a person's own chat titled the same
    ];
    expect(chats.withoutCanonical(rows).map((r) => r.id)).toEqual(["side-chat", "plain"]);
  });

  it("the surface's disable switch unregisters it live", () => {
    const registry = new ContributionRegistry(); // the P24-32 registry the app uses
    const navIds = () => registry.list("sidebar-nav").map((c) => c.navId);
    const routePaths = () => registry.list("route").map((c) => c.path);
    let changes = 0;
    registry.subscribe(() => {
      changes += 1;
    });
    const surface = new BotsSurface(
      registryHost(registry),
      botsContribution(() => null, "Bots"),
    );
    surface.setEnabled(true);
    expect(navIds()).toEqual(["bots"]);
    expect(routePaths()).toEqual(["/bots"]);
    expect(registry.bySource("app")).toHaveLength(2);
    surface.setEnabled(true); // idempotent
    surface.setEnabled(false);
    expect(navIds()).toEqual([]);
    expect(routePaths()).toEqual([]);
    expect(surface.enabled()).toBe(false);
    expect(changes).toBe(2);
  });
});
