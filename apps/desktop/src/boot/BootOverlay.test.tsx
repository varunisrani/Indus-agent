// @vitest-environment jsdom
import { act, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router";
import { describe, expect, it, vi } from "vitest";
import { FakeTimers } from "../../testing/fakeTimers.ts";
import { seeded } from "../../testing/seeded.ts";
import type { IndusBootProgress, IndusInstallerEvent } from "../global.d.ts";
import { type BackendNotice, type BootBridge, BootOverlay } from "./BootOverlay.tsx";
import { BOOT_RETRY_CAP_MS, BOOT_RETRY_MAX_ATTEMPTS } from "./bootRetry.ts";

function progress(phase: IndusBootProgress["phase"], extra: Partial<IndusBootProgress> = {}) {
  return { phase, message: "", progress: 0.5, running: true, ...extra } as IndusBootProgress;
}

function fakeBoot(initial: IndusBootProgress) {
  let onProgress: ((p: IndusBootProgress) => void) | null = null;
  let onInstaller: ((e: IndusInstallerEvent) => void) | null = null;
  const bridge = {
    get: vi.fn(async () => initial),
    onProgress: (listener: (p: IndusBootProgress) => void) => {
      onProgress = listener;
      return () => undefined;
    },
    onInstaller: (listener: (e: IndusInstallerEvent) => void) => {
      onInstaller = listener;
      return () => undefined;
    },
    retry: vi.fn(async () => ({ accepted: true })),
    choose: vi.fn(async () => ({ accepted: true })),
    cancelInstall: vi.fn(async () => ({ accepted: true })),
  };
  return {
    bridge: bridge as unknown as BootBridge & typeof bridge,
    push: (p: IndusBootProgress) => act(() => onProgress?.(p)),
    install: (e: IndusInstallerEvent) => act(() => onInstaller?.(e)),
  };
}

function Where() {
  return <span data-testid="where">{useLocation().pathname}</span>;
}

async function mount(initial: IndusBootProgress, options: { timers?: FakeTimers } = {}) {
  const boot = fakeBoot(initial);
  const onReady = vi.fn();
  const timers = options.timers ?? new FakeTimers();
  const clock = {
    now: () => timers.now,
    setTimeout: (run: () => void, ms: number) => timers.setTimeout(run, ms),
    clearTimeout: (handle: unknown) => timers.clearTimeout(handle),
  };
  const random = seeded(3);
  render(
    <MemoryRouter initialEntries={["/"]}>
      <BootOverlay bridge={boot.bridge} onReady={onReady} clock={clock} random={random} />
      <Where />
    </MemoryRouter>,
  );
  await act(async () => {
    await Promise.resolve();
  });
  return { ...boot, onReady, timers };
}

describe("BootOverlay", () => {
  it.each([
    ["resolve", "Looking for Indus on this computer"],
    ["runtime", "Starting Indus"],
    ["remote-connect", "Connecting to your Indus"],
    ["update-wait", "Waiting for an update to finish"],
  ] as const)("renders the %s phase", async (phase, text) => {
    await mount(progress(phase));
    expect(screen.getByRole("heading", { name: text })).toBeTruthy();
    expect(screen.getByRole("dialog")).toBeTruthy();
  });

  it("goes away at ready and reports it once", async () => {
    const boot = await mount(progress("runtime"));
    boot.push(progress("ready", { running: false }));
    expect(screen.queryByRole("dialog")).toBeNull();
    boot.push(progress("ready", { running: false, message: "again" }));
    expect(boot.onReady).toHaveBeenCalledTimes(1);
  });

  it("shows the installer's stages and the log tail", async () => {
    const boot = await mount(progress("runtime"));
    boot.install({
      type: "manifest",
      stages: [
        { name: "python", title: "Python runtime", category: "core", needsUserInput: false },
        { name: "indus", title: "Indus package", category: "core", needsUserInput: false },
      ],
    });
    boot.install({ type: "stage", name: "python", state: "succeeded" });
    boot.install({ type: "stage", name: "indus", state: "running" });
    for (let i = 0; i < 20; i += 1)
      boot.install({ type: "log", line: `line ${i}`, stream: "stdout" });
    expect(screen.getByRole("heading", { name: "Installing Indus" })).toBeTruthy();
    const stages = screen.getAllByRole("listitem");
    expect(stages.map((li) => li.getAttribute("data-state"))).toEqual(["succeeded", "running"]);
    expect(screen.getByText(/line 19/)).toBeTruthy();
    expect(screen.queryByText(/line 7$/m)).toBeNull();
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    expect(screen.getByRole("heading", { name: "Installing Indus" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Cancel install" }));
    expect(boot.bridge.cancelInstall).toHaveBeenCalledTimes(1);
  });

  it("first run offers both choices, ignores Escape and sends the choice", async () => {
    const boot = await mount(progress("first-run"));
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    expect(screen.getByRole("heading", { name: "How do you want to use Indus?" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /Install on this computer/ }));
    expect(boot.bridge.choose).toHaveBeenCalledWith({ choice: "install-local" });
    expect(screen.getByRole("button", { name: /Connect to an existing Indus/ })).toBeTruthy();
  });

  it("retries a retryable failure itself, at most five times, then shows the overlay", async () => {
    const failure = progress("error", { error: "dial failed", retryable: true, running: false });
    const boot = await mount(failure);
    for (let attempt = 1; attempt <= BOOT_RETRY_MAX_ATTEMPTS; attempt += 1) {
      expect(screen.queryByRole("heading", { name: "Indus could not start" })).toBeNull();
      expect(screen.getByText(new RegExp(`attempt ${attempt} of 5`))).toBeTruthy();
      act(() => boot.timers.advance(BOOT_RETRY_CAP_MS));
      await act(async () => {
        await Promise.resolve();
      });
      expect(boot.bridge.retry).toHaveBeenCalledTimes(attempt);
      // Main reports the same failure again after each attempt.
      boot.push({ ...failure, message: `after ${attempt}` });
    }
    expect(screen.getByRole("heading", { name: "Indus could not start" })).toBeTruthy();
    act(() => boot.timers.advance(10 * BOOT_RETRY_CAP_MS));
    expect(boot.bridge.retry).toHaveBeenCalledTimes(BOOT_RETRY_MAX_ATTEMPTS);
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(boot.bridge.retry).toHaveBeenLastCalledWith({ action: "retry" });
  });

  it("a latched failure shows Repair at once and is not retried automatically", async () => {
    const boot = await mount(
      progress("error", {
        error: "runtime missing",
        retryable: true,
        latched: true,
        running: false,
      }),
    );
    expect(screen.getByRole("heading", { name: "Indus could not start" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
    act(() => boot.timers.advance(10 * BOOT_RETRY_CAP_MS));
    expect(boot.bridge.retry).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Repair" }));
    expect(boot.bridge.retry).toHaveBeenCalledWith({ action: "repair" });
  });

  it("Settings opens the settings overlay and gives it the window", async () => {
    await mount(progress("error", { error: "no runtime", retryable: false, running: false }));
    fireEvent.click(screen.getByRole("button", { name: "Settings" }));
    expect(screen.getByTestId("where").textContent).toBe("/settings");
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("shows a crashing backend's notice with its last output on the failure view", async () => {
    let push: ((notice: BackendNotice) => void) | null = null;
    const boot = fakeBoot(progress("error", { error: "backend stopped", running: false }));
    render(
      <MemoryRouter>
        <BootOverlay
          bridge={boot.bridge}
          notices={(listener) => {
            push = listener;
            return () => undefined;
          }}
        />
      </MemoryRouter>,
    );
    await act(async () => {
      await Promise.resolve();
    });
    act(() =>
      push?.({
        kind: "crashing",
        message: "Indus keeps stopping",
        exitCode: 3,
        recentOutput: ["first", "Traceback: boom"],
      }),
    );
    expect(screen.getByText("Indus keeps stopping (exit code 3)")).toBeTruthy();
    expect(screen.getByText(/Traceback: boom/)).toBeTruthy();
  });

  it("a refused credential (401) asks to sign in again", async () => {
    await mount(progress("error", { error: "unauthorized", statusCode: 401, running: false }));
    expect(screen.getByRole("heading", { name: "Sign in again" })).toBeTruthy();
  });

  it("a newer progress event wins over a late boot.get answer", async () => {
    const boot = fakeBoot(progress("resolve"));
    let answer: (p: IndusBootProgress) => void = () => undefined;
    boot.bridge.get.mockImplementation(
      () =>
        new Promise<IndusBootProgress>((resolve) => {
          answer = resolve;
        }),
    );
    render(
      <MemoryRouter>
        <BootOverlay bridge={boot.bridge} />
      </MemoryRouter>,
    );
    boot.push(progress("remote-connect"));
    await act(async () => {
      answer(progress("resolve"));
      await Promise.resolve();
    });
    expect(screen.getByRole("heading", { name: "Connecting to your Indus" })).toBeTruthy();
  });

  it("without a main process boot is treated as ready", async () => {
    const onReady = vi.fn();
    render(
      <MemoryRouter>
        <BootOverlay bridge={null} onReady={onReady} />
      </MemoryRouter>,
    );
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(onReady).toHaveBeenCalledTimes(1);
  });
});
