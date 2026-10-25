import { NotConnected } from "@indus/client";
import { describe, expect, it, vi } from "vitest";
import {
  armWhenReady,
  type DictateRequest,
  dictationDone,
  installAppWake,
  requestDictation,
  WAKE_DICTATE_EVENT,
} from "./appWake.ts";

function rig() {
  const target = new EventTarget();
  const listeners = new Set<(event: { type?: string }) => void>();
  const calls: string[] = [];
  const rpc = {
    request: vi.fn(async (method: string) => {
      calls.push(method);
      if (method === "wake.start") return { started: true, phrase: "hey indus" };
      if (method === "wake.resume") return { ok: true };
      return {};
    }),
    onEvent: (listener: (event: { type?: string }) => void) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  };
  const lights: string[] = [];
  const made = installAppWake(() => rpc, target);
  made.controller.subscribe(() => lights.push(made.controller.get().indicator));
  const emit = (type: string) => {
    for (const listener of [...listeners]) listener({ type });
  };
  return { target, made, calls, emit, lights };
}

describe("the window's wake word hands off to the composer's dictation (P24-20 → P24-35)", () => {
  it("a detection starts dictation on screen; its end listens again", async () => {
    const { target, made, calls, emit } = rig();
    const taken: number[] = [];
    target.addEventListener(WAKE_DICTATE_EVENT, (event) => {
      (event as CustomEvent<DictateRequest>).detail.taken = true;
      taken.push(1);
    });
    await made.controller.start({ consent: false });
    emit("wake.detected");
    expect(taken).toEqual([1]);
    expect(made.controller.get().indicator).toBe("capturing");
    dictationDone(target);
    await vi.waitFor(() => expect(made.controller.get().indicator).toBe("listening"));
    expect(calls).toEqual(["wake.start", "wake.resume"]);
    made.dispose();
  });

  it("with no composer on screen the controller listens again at once", async () => {
    const { made, calls, emit } = rig();
    await made.controller.start({ consent: false });
    emit("wake.detected");
    await vi.waitFor(() => expect(made.controller.get().indicator).toBe("listening"));
    expect(calls).toEqual(["wake.start", "wake.resume"]);
    made.dispose();
  });

  it("requestDictation throws when nobody takes it", () => {
    expect(() => requestDictation(new EventTarget())).toThrow(/no composer/);
  });
});

describe("the launch arm waits for the connection (W54-D2-3)", () => {
  function connection(ready: boolean) {
    const listeners = new Set<() => void>();
    let state = { ready };
    return {
      getState: () => state,
      subscribe: (listener: () => void) => {
        listeners.add(listener);
        return () => listeners.delete(listener);
      },
      becomeReady: () => {
        state = { ready: true };
        for (const listener of [...listeners]) listener();
      },
      listeners,
    };
  }

  it("asks wake.start only after the daemon is ready, once", async () => {
    const { made, calls } = rig();
    const link = connection(false);
    armWhenReady(made.controller, link);
    expect(calls).toEqual([]);
    link.becomeReady();
    link.becomeReady();
    await vi.waitFor(() => expect(calls).toEqual(["wake.start"]));
    expect(link.listeners.size).toBe(0);
    made.dispose();
  });

  it("a launch arm that finds no connection leaves no error under the switch", async () => {
    const made = installAppWake(
      () => ({
        request: async () => {
          throw new NotConnected();
        },
      }),
      new EventTarget(),
    );
    await made.controller.start({ consent: false });
    expect(made.controller.get().notice).toBeNull();
    expect(made.controller.get().listening).toBe(false);
    // the person's own switch still reports the failure
    await made.controller.start({ consent: true });
    expect(made.controller.get().notice?.key).toBe("wake.reason.failed");
    made.dispose();
  });

  it("at launch, an answer of 'off' is the normal state, not a notice; the switch still hears it", async () => {
    const made = installAppWake(
      () => ({ request: async () => ({ started: false, reason: "disabled" }) }),
      new EventTarget(),
    );
    await made.controller.start({ consent: false });
    expect(made.controller.get().notice).toBeNull();
    await made.controller.start({ consent: true });
    expect(made.controller.get().notice?.key).toBe("wake.reason.disabled");
    made.dispose();
  });
});
