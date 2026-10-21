import { describe, expect, it } from "vitest";
import { gpuCrashDecision, MAX_LISTED, QuitGuard, quitPrompt, relaunchArgs } from "./quitGuard.ts";

const items = (n: number) =>
  Array.from({ length: n }, (_, i) => ({ id: `s${i}`, label: `Chat ${i + 1}` }));

describe("quit guard", () => {
  it("nothing running → quit at once", () => {
    expect(quitPrompt([], [])).toBeNull();
  });

  it("attached local daemon with running turns → informational prompt, nothing is stopped", () => {
    const prompt = quitPrompt(items(1), []);
    expect(prompt).toEqual({
      message: "The agent is still working on 1 chat.",
      detail:
        "• Chat 1\n\nThe agent keeps working after you quit. Open Indus again later to see the results.",
      buttons: ["Cancel", "Quit"],
      informational: true,
    });
  });

  it("app-owned work asks to confirm, listing up to 4 items and the rest as a count", () => {
    const prompt = quitPrompt(items(2), items(6));
    expect(prompt?.informational).toBe(false);
    expect(prompt?.buttons).toEqual(["Keep running", "Quit anyway"]);
    expect(prompt?.message).toBe("Indus is still busy with 6 tasks.");
    const lines = prompt?.detail.split("\n") ?? [];
    expect(lines.filter((line) => line.startsWith("• Chat"))).toHaveLength(MAX_LISTED);
    expect(lines).toContain("• 2 more");
    expect(quitPrompt([], items(5))?.detail).toContain("• 1 more");
  });

  it("reports merge across windows by id; a closed window stops counting", () => {
    const guard = new QuitGuard();
    guard.report(1, [{ id: "a", label: "Alpha" }]);
    guard.report(2, [
      { id: "a", label: "Alpha" },
      { id: "b", label: " Beta " },
    ]);
    expect(guard.work()).toEqual([
      { id: "a", label: "Alpha" },
      { id: "b", label: "Beta" },
    ]);
    guard.forget(2);
    expect(guard.work()).toEqual([{ id: "a", label: "Alpha" }]);
    guard.report(1, []);
    expect(guard.work()).toEqual([]);
    expect(quitPrompt([{ id: "x", label: "" }], [])?.detail).toContain("• Untitled chat");
  });
});

describe("gpu crash relaunch", () => {
  const crash = { type: "GPU", reason: "crashed", exitCode: -1073740791 };

  it("a known GPU crash signature → one relaunch with the GPU disabled", () => {
    expect(gpuCrashDecision(crash, "1.0.0", null, ["--x"])).toBe("relaunch-without-gpu");
    expect(
      gpuCrashDecision({ ...crash, reason: "launch-failed", exitCode: 0 }, "1.0.0", null, []),
    ).toBe("relaunch-without-gpu");
  });

  it("bounded: not twice per version, not when already without GPU, not for other children", () => {
    expect(gpuCrashDecision(crash, "1.0.0", { version: "1.0.0" }, [])).toBe("ignore");
    expect(gpuCrashDecision(crash, "1.0.1", { version: "1.0.0" }, [])).toBe("relaunch-without-gpu");
    expect(gpuCrashDecision(crash, "1.0.0", null, ["--disable-gpu"])).toBe("ignore");
    expect(gpuCrashDecision({ ...crash, type: "Utility" }, "1.0.0", null, [])).toBe("ignore");
    expect(gpuCrashDecision({ ...crash, reason: "killed" }, "1.0.0", null, [])).toBe("ignore");
    expect(gpuCrashDecision({ ...crash, exitCode: 1 }, "1.0.0", null, [])).toBe("ignore");
  });

  it("the relaunch never turns the sandbox off", () => {
    const args = relaunchArgs(["--enable-logging"]);
    expect(args).toEqual(["--enable-logging", "--disable-gpu"]);
    expect(args).not.toContain("--no-sandbox");
  });
});
