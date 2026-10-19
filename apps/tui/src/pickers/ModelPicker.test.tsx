import { GatewayRpcError } from "@indus/client";
import { ERROR_CODES } from "@indus/protocol";
import { describe, expect, it, vi } from "vitest";
import { KEY, mount } from "../cards/inkHarness.ts";
import { ModelPicker } from "./ModelPicker.tsx";

const tick = (ms = 10): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));

const providers = Array.from({ length: 15 }, (_, i) => ({
  slug: `vendor${String(i + 1).padStart(2, "0")}`,
  name: `Vendor ${i + 1}`,
  auth_type: "api_key",
  authenticated: true,
  current: i === 0,
  models: [`model-${i + 1}a`, `model-${i + 1}b`],
}));

function fakeRpc(setResult: () => unknown) {
  const calls: [string, unknown][] = [];
  const call = vi.fn(async (method: string, params: unknown) => {
    calls.push([method, params]);
    if (method === "model.options") return { provider: "vendor01", model: "model-1a", providers };
    if (method === "config.set") return setResult();
    throw new Error(`unexpected ${method}`);
  });
  return { call, calls };
}

describe("model picker on screen", () => {
  it("shows 12 provider rows and a count of the rest", async () => {
    const rpc = fakeRpc(() => ({}));
    const app = await mount(
      <ModelPicker
        rpc={rpc}
        sessionId="s-1"
        onClose={() => {}}
        systemLine={() => {}}
        confirm={async () => true}
      />,
    );
    await tick();
    const frame = app.lastFrame();
    expect(rpc.calls[0]).toEqual(["model.options", { session_id: "s-1" }]);
    expect(frame).toContain("Vendor 1 (current)");
    expect(frame).toContain("Vendor 12");
    expect(frame).not.toContain("Vendor 13");
    expect(frame).toContain("↓ 3 more");
    expect(frame).toContain("session only");
    app.unmount();
  });

  it("walks provider → model → effort and applies; a deferred switch leaves a notice", async () => {
    const rpc = fakeRpc(() => ({ key: "model", value: "x", applied: "deferred", saved: false }));
    const lines: string[] = [];
    const onClose = vi.fn();
    const app = await mount(
      <ModelPicker
        rpc={rpc}
        sessionId="s-1"
        onClose={onClose}
        systemLine={(t) => lines.push(t)}
        confirm={async () => true}
      />,
    );
    await tick();
    await app.press(KEY.down, KEY.enter);
    expect(app.lastFrame()).toContain("Models of Vendor 2");
    await app.press(KEY.down, KEY.enter);
    expect(app.lastFrame()).toContain("Reasoning effort for model-2b");
    await app.press(KEY.enter);
    await tick();
    expect(rpc.calls[1]).toEqual([
      "config.set",
      { key: "model", value: "vendor02:model-2b", session_id: "s-1" },
    ]);
    expect(lines.join("\n")).toContain("when the current reply ends");
    expect(onClose).toHaveBeenCalledTimes(1);
    app.unmount();
  });

  it("confirm_required round-trips through the confirm hook", async () => {
    let first = true;
    const rpc = fakeRpc(() => {
      if (first) {
        first = false;
        throw new GatewayRpcError(ERROR_CODES.CONFIRM_REQUIRED.code, "expensive model");
      }
      return { key: "model", value: "x", applied: "now", saved: false };
    });
    const confirm = vi.fn(async () => true);
    const app = await mount(
      <ModelPicker
        rpc={rpc}
        sessionId="s-1"
        onClose={() => {}}
        systemLine={() => {}}
        confirm={confirm}
      />,
    );
    await tick();
    await app.press(KEY.enter, KEY.enter, KEY.enter);
    await tick();
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(rpc.calls.filter(([m]) => m === "config.set")).toHaveLength(2);
    expect(rpc.calls[2]?.[1]).toMatchObject({ confirm: true });
    app.unmount();
  });

  it("an options failure shows the error and Esc closes", async () => {
    const call = vi.fn(async () => {
      throw new Error("bridge not ready");
    });
    const onClose = vi.fn();
    const app = await mount(
      <ModelPicker
        rpc={{ call }}
        sessionId={null}
        onClose={onClose}
        systemLine={() => {}}
        confirm={async () => true}
      />,
    );
    await tick();
    expect(app.lastFrame()).toContain("bridge not ready");
    expect(call).toHaveBeenCalledWith("model.options", {});
    await app.press(KEY.escape);
    expect(onClose).toHaveBeenCalledTimes(1);
    app.unmount();
  });
});
