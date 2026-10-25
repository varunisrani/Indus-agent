// @vitest-environment jsdom
import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { profileTarget } from "../app/Shell.tsx";
import {
  type DispatchedAction,
  keyActionFor,
  useKeyAction,
  useKeyActionDispatch,
} from "./keyActions.ts";
import { KEY_ACTIONS, keyGroupOf } from "./keybindings.ts";

afterEach(cleanup);

const key = (
  k: string,
  mods: Partial<Record<"ctrl" | "meta" | "alt" | "shift", boolean>> = {},
) => ({
  key: k,
  ctrlKey: mods.ctrl === true,
  metaKey: mods.meta === true,
  altKey: mods.alt === true,
  shiftKey: mods.shift === true,
});

describe("rebindable key actions (W59-K1-2)", () => {
  it("default bindings run their action; an unset one runs nothing until bound", () => {
    expect(keyActionFor(key("m", { ctrl: true, shift: true }), {}, "linux")).toBe("model.picker");
    expect(keyActionFor(key("v", { ctrl: true, alt: true }), {}, "linux")).toBe(
      "voice.conversation",
    );
    expect(keyActionFor(key("n", { ctrl: true }), {}, "linux")).toBe("chat.new");
    expect(keyActionFor(key(",", { ctrl: true }), {}, "linux")).toBe("settings.open");
    expect(keyActionFor(key("l", { meta: true }), {}, "darwin")).toBe("composer.focus");
    expect(keyActionFor(key("ArrowUp", { ctrl: true, shift: true }), {}, "linux")).toBeNull();
    const bound = { "effort.up": "CommandOrControl+Shift+Up", "model.picker": "" };
    expect(keyActionFor(key("ArrowUp", { ctrl: true, shift: true }), bound, "linux")).toBe(
      "effort.up",
    );
    // an override that unbinds the default frees the key
    expect(keyActionFor(key("m", { ctrl: true, shift: true }), bound, "linux")).toBeNull();
  });

  it("pane keys, repeats and handled keydowns never run an action", () => {
    const onPaneKey = { "chat.new": "CommandOrControl+T" };
    expect(keyActionFor(key("t", { ctrl: true }), onPaneKey, "linux")).toBeNull();
    expect(keyActionFor({ ...key("n", { ctrl: true }), repeat: true }, {}, "linux")).toBeNull();
    expect(
      keyActionFor({ ...key("n", { ctrl: true }), defaultPrevented: true }, {}, "linux"),
    ).toBeNull();
    expect(keyActionFor(key("Control", { ctrl: true }), {}, "linux")).toBeNull();
  });

  it("a keydown reaches the action's owner and is marked handled", () => {
    const seen: DispatchedAction[] = [];
    function Owner() {
      useKeyActionDispatch({ "profile.next": "CommandOrControl+Alt+Down" }, "linux");
      useKeyAction(["profile.next", "model.picker"], (id) => {
        seen.push(id);
        return id === "profile.next";
      });
      return null;
    }
    render(<Owner />);
    const down = new KeyboardEvent("keydown", {
      key: "ArrowDown",
      ctrlKey: true,
      altKey: true,
      cancelable: true,
    });
    window.dispatchEvent(down);
    expect(seen).toEqual(["profile.next"]);
    expect(down.defaultPrevented).toBe(true);
    // an owner that could not act leaves the key to the page
    const picker = new KeyboardEvent("keydown", {
      key: "M",
      ctrlKey: true,
      shiftKey: true,
      cancelable: true,
    });
    window.dispatchEvent(picker);
    expect(seen).toEqual(["profile.next", "model.picker"]);
    expect(picker.defaultPrevented).toBe(false);
    const other = vi.fn();
    window.addEventListener("keydown", other);
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "q", ctrlKey: true }));
    window.removeEventListener("keydown", other);
    expect(seen).toHaveLength(2);
  });

  it("every new action is listed in its group", () => {
    const ids = KEY_ACTIONS.map((action) => action.id);
    for (const id of ["model.picker", "effort.up", "effort.down", "voice.dictation"]) {
      expect(ids).toContain(id);
      expect(keyGroupOf(id)).toBe("composer");
    }
    for (const id of ["profile.default", "profile.next", "profile.previous"]) {
      expect(keyGroupOf(id)).toBe("profiles");
    }
  });

  it("profile keys step through the rail and find the default", () => {
    const names = ["default", "research", "work"];
    expect(profileTarget("profile.next", names, "default", null)).toBe("research");
    expect(profileTarget("profile.next", names, "work", null)).toBe("default");
    expect(profileTarget("profile.previous", names, "default", null)).toBe("work");
    expect(profileTarget("profile.default", names, "work", "research")).toBe("research");
    expect(profileTarget("profile.default", names, "default", null)).toBeNull();
    expect(profileTarget("profile.next", ["default"], "default", null)).toBeNull();
  });
});
