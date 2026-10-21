// @vitest-environment jsdom
import { fireEvent, render, screen } from "@testing-library/react";
import axe from "axe-core";
import { describe, expect, it, vi } from "vitest";
import { ProfileRail, profileColor, profileInitial, profileNames } from "./ProfileRail.tsx";

function mount(foreground = "default", defaultProfile: string | null = null) {
  const handlers = { onSelect: vi.fn(), onOpenInWindow: vi.fn(), onSetDefault: vi.fn() };
  const view = render(
    <ProfileRail
      profiles={["default", "work", "client"]}
      foreground={foreground}
      defaultProfile={defaultProfile}
      {...handlers}
    />,
  );
  return { view, ...handlers };
}

describe("profile rail", () => {
  it("one square per profile; the foreground one is pressed; click swaps", () => {
    const { onSelect } = mount("work");
    expect(screen.getByRole("button", { name: "Profile work" }).getAttribute("aria-pressed")).toBe(
      "true",
    );
    fireEvent.click(screen.getByRole("button", { name: "Profile work" }));
    expect(onSelect).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Profile client" }));
    expect(onSelect).toHaveBeenCalledWith("client");
  });

  it("the default profile is badged", () => {
    mount("default", "client");
    expect(screen.getByRole("button", { name: "Profile client (default)" })).toBeTruthy();
  });

  it("context menu: open in new window, set as default", () => {
    const { onOpenInWindow, onSetDefault } = mount();
    fireEvent.contextMenu(screen.getByRole("button", { name: /Profile work/ }));
    const menu = screen.getByRole("menu", { name: "Profile work" });
    expect(document.activeElement?.textContent).toBe("Open in new window");
    fireEvent.click(screen.getByRole("menuitem", { name: "Open in new window" }));
    expect(onOpenInWindow).toHaveBeenCalledWith("work");
    expect(menu.isConnected).toBe(false);
    fireEvent.keyDown(screen.getByRole("button", { name: /Profile client/ }), {
      key: "F10",
      shiftKey: true,
    });
    fireEvent.click(screen.getByRole("menuitem", { name: "Set as default" }));
    expect(onSetDefault).toHaveBeenCalledWith("client");
  });

  it("Escape closes the menu; the default profile's entry says so", () => {
    const { onSetDefault } = mount("default", null);
    fireEvent.keyDown(screen.getByRole("button", { name: /Profile default/ }), {
      key: "ContextMenu",
    });
    const item = screen.getByRole("menuitem", { name: "Default profile" });
    fireEvent.click(item);
    expect(onSetDefault).not.toHaveBeenCalled();
    fireEvent.keyDown(screen.getByRole("button", { name: /Profile work/ }), { key: "ContextMenu" });
    fireEvent.keyDown(screen.getByRole("menu"), { key: "Escape" });
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("colours and initials are stable; the list sorts default first", () => {
    expect(profileColor("work")).toBe(profileColor("work"));
    expect(profileColor("work")).toMatch(/^oklch\(/);
    expect(profileInitial("work")).toBe("W");
    expect(
      profileNames({ profiles: [{ name: "b" }, { name: "default" }, { name: "a" }, {}] }),
    ).toEqual(["default", "a", "b"]);
    expect(profileNames(null)).toEqual([]);
  });

  it("has no axe violations", async () => {
    const { view } = mount("work");
    const results = await axe.run(view.container, {
      rules: { "color-contrast": { enabled: false } },
    });
    expect(results.violations.map((v) => `${v.id}: ${v.help}`)).toEqual([]);
  });
});
