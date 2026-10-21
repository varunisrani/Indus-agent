// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { IndusDesktopUninstall, IndusUninstallSummary } from "../global.d.ts";
import { UninstallSection } from "./UninstallSection.tsx";

const ALLOWED: IndusUninstallSummary = {
  allowed: true,
  levels: ["gui", "keep-data", "full"],
  installKind: "git",
  home: "/home/u/.indus",
  confirmPhrase: "uninstall everything",
};

function bridge(summary: IndusUninstallSummary) {
  const run = vi.fn(async () => ({ ok: true as const }));
  const api: IndusDesktopUninstall = { summary: async () => summary, run };
  return { api, run };
}

async function show(summary: IndusUninstallSummary) {
  const rig = bridge(summary);
  render(<UninstallSection uninstall={rig.api} />);
  await act(async () => {});
  return rig;
}

afterEach(() => cleanup());

describe("UninstallSection", () => {
  it("a Nix-owned receipt hides all options and shows why", async () => {
    await show({
      allowed: false,
      levels: [],
      reason: "Nix installed this Indus.",
      installKind: "nix",
      home: "/h",
      confirmPhrase: "uninstall everything",
    });
    expect(screen.getByTestId("uninstall-hidden").textContent).toContain("Nix");
    expect(screen.queryAllByRole("button")).toHaveLength(0);
  });

  it('"Remove everything" needs the exact phrase before anything runs', async () => {
    const { run } = await show(ALLOWED);
    fireEvent.click(screen.getByText("Remove everything"));
    const confirm = screen.getByText("Quit and uninstall") as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);
    fireEvent.click(confirm);
    expect(run).not.toHaveBeenCalled();
    const field = screen.getByLabelText(/Type uninstall everything to confirm/);
    fireEvent.change(field, { target: { value: "uninstall every" } });
    expect(confirm.disabled).toBe(true);
    fireEvent.change(field, { target: { value: "uninstall everything" } });
    expect(confirm.disabled).toBe(false);
    await act(async () => {
      fireEvent.click(confirm);
    });
    expect(run).toHaveBeenCalledWith({ level: "full", typed: "uninstall everything" });
  });

  it("the app-only level asks once, then runs", async () => {
    const { run } = await show(ALLOWED);
    fireEvent.click(screen.getByText("Remove the app only"));
    expect(screen.getByRole("alertdialog").textContent).toContain("quit now");
    await act(async () => {
      fireEvent.click(screen.getByText("Quit and uninstall"));
    });
    expect(run).toHaveBeenCalledWith({ level: "gui" });
  });

  it("a refusal from main is shown", async () => {
    const rig = bridge(ALLOWED);
    rig.run.mockResolvedValueOnce({ ok: false, code: "not-owned", message: "not ours" } as never);
    render(<UninstallSection uninstall={rig.api} />);
    await act(async () => {});
    fireEvent.click(screen.getByText("Remove the app only"));
    await act(async () => {
      fireEvent.click(screen.getByText("Quit and uninstall"));
    });
    expect(screen.getByRole("alert").textContent).toContain("not ours");
  });
});
