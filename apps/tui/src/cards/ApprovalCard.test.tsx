import { describe, expect, it, vi } from "vitest";
import {
  ApprovalCard,
  approvalChoices,
  approvalStep,
  CHOICE_ORDER,
  commandPreview,
} from "./ApprovalCard.tsx";
import { KEY, mount } from "./inkHarness.ts";

const numbered = (n: number): string =>
  Array.from({ length: n }, (_, i) => `echo step-${i + 1}`).join("\n");

describe("approval choices", () => {
  it("offers all four in canonical order by default", () => {
    expect(approvalChoices({})).toEqual(["once", "session", "always", "deny"]);
  });

  it("keeps the canonical order whatever order the request lists", () => {
    expect(approvalChoices({ choices: ["deny", "always", "once"] })).toEqual([
      "once",
      "always",
      "deny",
    ]);
  });

  it("always keeps deny", () => {
    expect(approvalChoices({ choices: ["once"] })).toEqual(["once", "deny"]);
  });

  it("offers only once and deny after a smart denial", () => {
    expect(approvalChoices({ smart_denied: true })).toEqual(["once", "deny"]);
  });

  it("drops always when a permanent allow is not allowed", () => {
    expect(approvalChoices({ allow_permanent: false })).toEqual(["once", "session", "deny"]);
  });
});

describe("command preview", () => {
  it("shows ten of twenty-five lines and counts the rest", () => {
    const preview = commandPreview(numbered(25), 80);
    expect(preview.rows).toHaveLength(10);
    expect(preview.rows[9]).toBe("echo step-10");
    expect(preview.hidden).toBe(15);
  });

  it("wraps a long line to cols - 8 instead of clipping it", () => {
    const long = "x".repeat(150);
    const preview = commandPreview(long, 80);
    expect(preview.rows.map((r) => r.length)).toEqual([72, 72, 6]);
    expect(preview.rows.join("")).toBe(long);
  });

  it("never wraps narrower than 20 columns", () => {
    expect(commandPreview("y".repeat(40), 10).rows).toEqual(["y".repeat(20), "y".repeat(20)]);
  });

  it("counts wide characters as two columns", () => {
    const rows = commandPreview("界".repeat(15), 28).rows;
    expect(rows).toEqual(["界".repeat(10), "界".repeat(5)]);
  });
});

describe("approval keys", () => {
  const all = CHOICE_ORDER;

  it("digits pick by number", () => {
    expect(approvalStep("1", {}, 0, all)).toEqual({ type: "choose", choice: "once" });
    expect(approvalStep("4", {}, 0, all)).toEqual({ type: "choose", choice: "deny" });
  });

  it("ignores digits out of range and letters (R05.11)", () => {
    expect(approvalStep("5", {}, 0, all)).toEqual({ type: "none" });
    expect(approvalStep("y", {}, 0, all)).toEqual({ type: "none" });
    expect(approvalStep("a", {}, 0, all)).toEqual({ type: "none" });
  });

  it("Esc and Ctrl+C deny", () => {
    expect(approvalStep("", { escape: true }, 1, all)).toEqual({ type: "choose", choice: "deny" });
    expect(approvalStep("c", { ctrl: true }, 1, all)).toEqual({ type: "choose", choice: "deny" });
  });

  it("arrows move within bounds and Enter takes the highlighted row", () => {
    expect(approvalStep("", { upArrow: true }, 0, all)).toEqual({ type: "none" });
    expect(approvalStep("", { downArrow: true }, 0, all)).toEqual({ type: "move", index: 1 });
    expect(approvalStep("", { downArrow: true }, 3, all)).toEqual({ type: "none" });
    expect(approvalStep("", { return: true }, 2, all)).toEqual({
      type: "choose",
      choice: "always",
    });
  });
});

describe("approval card on screen", () => {
  const request = (command: string, choices = [...CHOICE_ORDER]) => ({
    command,
    description: "runs a shell command",
    choices,
  });

  it("shows 10 lines and '15 more lines' for a 25-line command at 80 columns", async () => {
    const app = await mount(
      <ApprovalCard request={request(numbered(25))} columns={80} onChoose={() => {}} />,
    );
    const frame = app.lastFrame();
    expect(frame).toContain("echo step-10");
    expect(frame).not.toContain("echo step-11");
    expect(frame).toContain("15 more lines");
    expect(frame).toContain("runs a shell command");
    app.unmount();
  });

  it("a digit answers at once", async () => {
    const onChoose = vi.fn();
    const app = await mount(
      <ApprovalCard request={request("ls")} columns={80} onChoose={onChoose} />,
    );
    await app.press("2");
    expect(onChoose).toHaveBeenCalledWith("session");
    app.unmount();
  });

  it("arrows then Enter, and Esc denies", async () => {
    const onChoose = vi.fn();
    const app = await mount(
      <ApprovalCard request={request("ls")} columns={80} onChoose={onChoose} />,
    );
    await app.press(KEY.down, KEY.down, KEY.enter);
    expect(onChoose).toHaveBeenLastCalledWith("always");
    await app.press(KEY.escape);
    expect(onChoose).toHaveBeenLastCalledWith("deny");
    app.unmount();
  });

  it("a smart denial shows two options", async () => {
    const app = await mount(
      <ApprovalCard
        request={request("rm -rf build", approvalChoices({ smart_denied: true }))}
        columns={80}
        onChoose={() => {}}
      />,
    );
    const frame = app.lastFrame();
    expect(frame).toContain("1. Allow once");
    expect(frame).toContain("2. Deny");
    expect(frame).not.toContain("3.");
    app.unmount();
  });
});
