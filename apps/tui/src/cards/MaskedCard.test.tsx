import { describe, expect, it, vi } from "vitest";
import { KEY, mount } from "./inkHarness.ts";
import { MaskedCard, maskedStep } from "./MaskedCard.tsx";

describe("masked card", () => {
  it("never paints the typed value and submits it on Enter", async () => {
    const onSubmit = vi.fn();
    const app = await mount(
      <MaskedCard title="Administrator password needed" onSubmit={onSubmit} />,
    );
    const value = ["pa", "ss", "-", "w0rd"].join("");
    await app.press(...value.split(""));
    for (const frame of app.frames()) {
      expect(frame).not.toContain(value);
      expect(frame).not.toContain("w0rd");
      expect(frame).not.toContain("ss-w");
    }
    expect(app.lastFrame()).toContain("•".repeat(value.length));
    await app.press(KEY.enter);
    expect(onSubmit).toHaveBeenCalledWith(value);
    app.unmount();
  });

  it("a pasted chunk counts whole and Backspace takes one character", async () => {
    const onSubmit = vi.fn();
    const app = await mount(<MaskedCard title="key" onSubmit={onSubmit} />);
    await app.press("abcdef", KEY.backspace, KEY.enter);
    expect(onSubmit).toHaveBeenCalledWith("abcde");
    app.unmount();
  });

  it("Esc sends nothing", async () => {
    const onSubmit = vi.fn();
    const app = await mount(<MaskedCard title="key" onSubmit={onSubmit} />);
    await app.press("x", "y", KEY.escape);
    expect(onSubmit).toHaveBeenCalledWith(null);
    app.unmount();
  });

  it("Ctrl+C sends nothing, and Enter on empty input sends nothing", () => {
    expect(maskedStep("c", { ctrl: true })).toEqual({ type: "cancel" });
    expect(maskedStep("u", { ctrl: true })).toEqual({ type: "clear" });
    expect(maskedStep("", { return: true })).toEqual({ type: "submit" });
  });

  it("only one answer is sent", async () => {
    const onSubmit = vi.fn();
    const app = await mount(<MaskedCard title="key" onSubmit={onSubmit} />);
    await app.press("z", KEY.enter, KEY.enter, KEY.escape);
    expect(onSubmit).toHaveBeenCalledTimes(1);
    app.unmount();
  });
});
