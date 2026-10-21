// @vitest-environment jsdom
import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { FakeTimers } from "../../testing/fakeTimers.ts";
import type { IndusWakeIndicatorState } from "../global.d.ts";
import { I18nProvider } from "../i18n/index.tsx";
import type { LooseRpc } from "../pet/PetOverlay.tsx";
import {
  WAKE_START_TIMEOUT_MS,
  WakeController,
  WakeIndicator,
  WakeToggle,
} from "./WakeIndicator.tsx";

type Answer = unknown | (() => Promise<unknown>);

function rpc(answers: Record<string, Answer>) {
  const calls: Array<{ method: string; params: object; options?: { timeoutMs?: number } }> = [];
  let listener: ((event: { type?: string }) => void) | null = null;
  const fake: LooseRpc & { emit(type: string): void } = {
    request: (method, params, options) => {
      calls.push({ method, params, ...(options === undefined ? {} : { options }) });
      const answer = answers[method];
      if (typeof answer === "function") return (answer as () => Promise<unknown>)();
      return Promise.resolve(answer ?? {});
    },
    onEvent: (fn) => {
      listener = fn;
      return () => {
        listener = null;
      };
    },
    emit: (type) => listener?.({ type }),
  };
  return { fake, calls };
}

function controller(answers: Record<string, Answer>) {
  const timers = new FakeTimers();
  const lights: IndusWakeIndicatorState[] = [];
  const handOff = vi.fn();
  const { fake, calls } = rpc(answers);
  const wake = new WakeController({
    rpc: fake,
    handOff,
    setIndicator: (state) => lights.push(state),
    timers,
  });
  return { wake, timers, lights, handOff, calls, fake };
}

const flush = async () => {
  for (let i = 0; i < 5; i++) await Promise.resolve();
};

afterEach(() => {
  delete (globalThis as { indusDesktop?: unknown }).indusDesktop;
});

describe("WakeController", () => {
  it("waits 180 s for the first start, then gives up and turns the light off", async () => {
    expect(WAKE_START_TIMEOUT_MS).toBe(180_000);
    const { wake, timers, calls, lights } = controller({
      "wake.start": () => new Promise(() => {}), // a first start loading a model, never answering
    });
    void wake.start({ consent: true });
    await flush();
    expect(calls[0]).toEqual({
      method: "wake.start",
      params: { surface: "gui", consent: true },
      options: { timeoutMs: 180_000 },
    });
    timers.advance(WAKE_START_TIMEOUT_MS - 1);
    await flush();
    expect(wake.get().pending).toBe(true);
    timers.advance(1);
    await flush();
    expect(wake.get()).toMatchObject({
      pending: false,
      listening: false,
      notice: { key: "wake.reason.timeout" },
    });
    expect(lights).toEqual([]);
  });

  it("a slow start inside the 180 s still arms", async () => {
    let release: (value: unknown) => void = () => {};
    const { wake, timers, lights } = controller({
      "wake.start": () =>
        new Promise((resolve) => {
          release = resolve;
        }),
    });
    void wake.start({ consent: true });
    timers.advance(179_000);
    release({ started: true, phrase: "hey indus" });
    await flush();
    expect(wake.get()).toMatchObject({ listening: true, pending: false, indicator: "listening" });
    expect(lights).toEqual(["listening"]);
    expect(timers.pending()).toBe(0);
  });

  it("refusals keep the light off and say why", async () => {
    const { wake, lights } = controller({
      "wake.start": { started: false, reason: "unavailable", hint: "no model is set" },
    });
    await wake.start({ consent: false });
    expect(wake.get().notice).toEqual({
      key: "wake.reason.unavailable",
      vars: { hint: "no model is set" },
    });
    expect(lights).toEqual([]);
  });

  it("a detection lights 'heard', hands off, records, then listens again", async () => {
    const { wake, lights, handOff, fake, calls } = controller({
      "wake.start": { started: true },
      "wake.resume": { ok: true },
    });
    const off = wake.attach(fake.onEvent);
    await wake.start({ consent: true });
    fake.emit("wake.detected");
    fake.emit("wake.detected"); // a second event while capturing changes nothing
    expect(handOff).toHaveBeenCalledTimes(1);
    expect(wake.get().indicator).toBe("capturing");
    await wake.captureDone();
    expect(lights).toEqual(["listening", "heard", "capturing", "listening"]);
    expect(calls.map((c) => c.method)).toEqual(["wake.start", "wake.resume"]);
    await wake.stop({ persist: true });
    expect(lights.at(-1)).toBe("hidden");
    expect(wake.get().enabled).toBe(false);
    off();
  });
});

describe("indicator and toggle", () => {
  it("the indicator window follows main's state", async () => {
    let push: (payload: { state: IndusWakeIndicatorState }) => void = () => {};
    (globalThis as { indusDesktop?: unknown }).indusDesktop = {
      wake: {
        getIndicator: vi.fn(async () => ({ state: "hidden" })),
        onIndicator: (fn: typeof push) => {
          push = fn;
          return () => {};
        },
      },
    };
    render(
      <I18nProvider>
        <WakeIndicator />
      </I18nProvider>,
    );
    await act(async () => flush());
    expect(screen.queryByTestId("wake-indicator")).toBeNull();
    act(() => push({ state: "listening" }));
    expect(screen.getByRole("status").textContent).toContain("hey indus");
    act(() => push({ state: "capturing" }));
    expect(screen.getByTestId("wake-indicator").dataset.state).toBe("capturing");
  });

  it("only the switch sends consent", async () => {
    const { wake, calls } = controller({ "wake.start": { started: true } });
    render(
      <I18nProvider>
        <WakeToggle controller={wake} />
      </I18nProvider>,
    );
    const toggle = screen.getByRole("switch");
    expect((toggle as HTMLInputElement).checked).toBe(false);
    await act(async () => {
      fireEvent.click(toggle);
      await flush();
    });
    expect(calls[0]?.params).toEqual({ surface: "gui", consent: true });
    expect((screen.getByRole("switch") as HTMLInputElement).checked).toBe(true);
    await act(async () => {
      fireEvent.click(screen.getByRole("switch"));
      await flush();
    });
    expect(calls.at(-1)).toEqual({ method: "wake.stop", params: { persist: true } });
  });
});
