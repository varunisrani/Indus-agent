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
    expect(screen.queryByTestId("uninstall-manual")).toBeNull(); // Nix's own way is in the reason
  });

  it("an unconfirmed install is no dead end: the CLI commands and the dev run are named (W54-D2-4)", async () => {
    const rig = bridge({
      allowed: false,
      levels: [],
      reason:
        "Indus could not confirm how it was installed, so it cannot remove itself. No receipt.",
      installKind: "unknown",
      home: "/h",
      confirmPhrase: "uninstall everything",
    });
    render(<UninstallSection uninstall={rig.api} dev />);
    await act(async () => {});
    expect(screen.getByTestId("uninstall-hidden").textContent).toContain("No receipt");
    expect(screen.getByTestId("uninstall-dev").textContent).toContain("development run");
    const manual = screen.getByTestId("uninstall-manual").textContent ?? "";
    expect(manual).toContain("indus uninstall --gui");
    expect(manual).toContain("indus uninstall --full");
    expect(screen.queryAllByRole("button")).toHaveLength(0); // nothing is run from here
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
