import { describe, expect, it } from "vitest";
import { createDispatcher } from "../slash/dispatch.ts";
import { slashSetup } from "../slash/testContext.ts";
import { startLine, statusText } from "./wake.ts";

describe("/wake", () => {
  it("on sends consent from the explicit command and says what happened", async () => {
    const { server, ctx, out } = slashSetup();
    server.handle("wake.start", () => ({ started: true, phrase: "hey indus" }));
    const deadlines: unknown[] = [];
    const inner = ctx.rpc;
    ctx.rpc = {
      call: (method, params, options) => {
        deadlines.push(options?.timeoutMs);
        return inner.call(method, params, options);
      },
    };
    await createDispatcher(ctx).run("/wake on");
    expect(server.calls("wake.start")[0]?.params).toEqual({ surface: "tui", consent: true });
    expect(deadlines).toEqual([190_000]); // a first start may load a model (server: 180 s)
    expect(out.lines.at(-1)).toContain("Listening for “hey indus”");
  });

  it("refusals are explained", async () => {
    const { server, ctx, out } = slashSetup();
    server.handle("wake.start", () => ({
      started: false,
      reason: "unavailable",
      hint: "No wake model is set",
    }));
    await createDispatcher(ctx).run("/wake on");
    expect(out.lines.at(-1)).toBe(
      "The wake word is not listening: it cannot run here (No wake model is set).",
    );
    expect(startLine({ started: false, reason: "owned" })).toContain("listening already");
  });

  it("off stops and persists; status shows the state", async () => {
    const { server, ctx, out } = slashSetup();
    server.handle("wake.stop", () => ({ ok: true }));
    server.handle("wake.status", () => ({
      enabled: true,
      consent: true,
      listening: false,
      owner_surface: "gui",
      phrase: "hey indus",
      engine: "openwakeword",
      available: true,
    }));
    await createDispatcher(ctx).run("/wake off");
    expect(server.calls("wake.stop")[0]?.params).toEqual({ persist: true });
    await createDispatcher(ctx).run("/wake");
    const shown = [...out.lines, ...out.pages.map((p) => p.text)].join("\n");
    expect(shown).toContain("owner: gui");
    expect(statusText({ enabled: false, consent: false })).toContain("no consent given yet");
  });

  it("an unknown word is a usage line, not a call", async () => {
    const { server, ctx, out } = slashSetup();
    await createDispatcher(ctx).run("/wake loudly");
    expect(out.lines).toEqual(["Use /wake on, /wake off or /wake status."]);
    expect(server.calls("wake.start")).toHaveLength(0);
  });
});
