// @vitest-environment jsdom
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import axe from "axe-core";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { LayoutStore, type StorageLike } from "./layoutStore.ts";
import {
  edgeAt,
  PANE_DRAG_TYPE,
  PaneRegistry,
  PaneRegistryProvider,
  PaneShell,
  slotLabel,
} from "./PaneShell.tsx";
import { allPanes, zoneOf } from "./paneTree.ts";
import { type ShortcutTimers, usePaneShortcuts } from "./shortcuts.ts";

function memory(): StorageLike {
  const data = new Map<string, string>();
  return {
    getItem: (key) => data.get(key) ?? null,
    setItem: (key, value) => {
      data.set(key, value);
    },
    removeItem: (key) => {
      data.delete(key);
    },
  };
}

/** A pane with state: a counter that would reset if the body were unmounted. */
function Counter(props: { name: string }) {
  const [n, setN] = useState(0);
  return (
    <button type="button" onClick={() => setN(n + 1)}>
      {props.name} {n}
    </button>
  );
}

function setup() {
  const registry = new PaneRegistry();
  registry.register({ id: "terminal", stateful: true, render: () => <Counter name="shell" /> });
  registry.register({ id: "preview", stateful: true, render: () => <Counter name="page" /> });
  registry.register({ id: "files", stateful: false, render: () => <p>file tree</p> });
  registry.register({ id: "review", stateful: false, render: () => <p>diffs</p> });
  const store = new LayoutStore({ storage: memory(), available: () => registry.ids() });
  const view = render(
    <PaneRegistryProvider registry={registry}>
      <PaneShell store={store} chat={<p>the chat</p>} sessionId="s1" profile="default" />
    </PaneRegistryProvider>,
  );
  return { registry, store, view };
}

afterEach(() => {
  vi.useRealTimers();
});

describe("pane shell", () => {
  it("renders zones with tabs and the chat body", () => {
    setup();
    expect(screen.getByText("the chat")).toBeTruthy();
    expect(screen.getAllByRole("tablist").length).toBeGreaterThanOrEqual(2);
    expect(screen.getByRole("tab", { name: "Chat" }).getAttribute("aria-selected")).toBe("true");
  });

  it("Hide keeps the stateful pane's DOM node (inert, hidden); showing it keeps its state", () => {
    setup();
    fireEvent.click(screen.getByText("shell 0"));
    const node = screen.getByText("shell 1").closest('[data-pane="terminal"]') as HTMLElement;
    fireEvent.click(screen.getByRole("button", { name: "Hide Terminal" }));
    const hidden = document.querySelector('[data-pane="terminal"]') as HTMLElement;
    expect(hidden).toBe(node);
    expect(hidden.hidden).toBe(true);
    expect(hidden.hasAttribute("inert")).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Show Terminal" }));
    expect(document.querySelector('[data-pane="terminal"]')).toBe(node);
    expect(screen.getByText("shell 1")).toBeTruthy();
  });

  it("a stateful pane behind another tab stays mounted; a plain one is not", () => {
    const { store } = setup();
    // the docs zone shows preview; files and review are behind it
    expect(document.querySelector('[data-pane="files"]')).toBeNull();
    act(() => store.activate("files"));
    expect(screen.getByText("file tree")).toBeTruthy();
    const preview = document.querySelector('[data-pane="preview"]') as HTMLElement;
    expect(preview.hidden).toBe(true);
  });

  it("minimise moves a zone to the restore rail and back; stateful zones offer Hide only", () => {
    const { store } = setup();
    act(() => store.activate("files"));
    expect(screen.queryByRole("button", { name: "Minimise Terminal" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Minimise Chat" })).toBeNull();
    // the docs zone holds the stateful preview: move files out to its own zone first
    const chatZone = zoneOf(store.layout().root, "chat")?.id ?? "";
    act(() => store.move("files", { zoneId: chatZone, edge: "right" }));
    fireEvent.click(screen.getByRole("button", { name: "Minimise Files" }));
    const rail = screen.getByRole("navigation", { name: "Minimised and hidden panes" });
    fireEvent.click(within(rail).getByRole("button", { name: "Restore Files" }));
    expect(screen.queryByRole("navigation", { name: "Minimised and hidden panes" })).toBeNull();
  });

  it("close and reopen from the toolbar; the chat cannot be closed", () => {
    const { store } = setup();
    act(() => store.activate("review"));
    fireEvent.click(screen.getByRole("button", { name: "Close Review" }));
    expect(allPanes(store.layout().root)).not.toContain("review");
    fireEvent.click(screen.getByRole("button", { name: "Open Review" }));
    expect(allPanes(store.layout().root)).toContain("review");
    expect(screen.queryByRole("button", { name: "Close Chat" })).toBeNull();
  });

  it("a tab dragged onto another zone joins its tabs", () => {
    const { store } = setup();
    const chatZone = document.querySelector(
      `[data-zone-id="${zoneOf(store.layout().root, "chat")?.id}"]`,
    ) as HTMLElement;
    const data = new Map<string, string>();
    const dataTransfer = {
      setData: (type: string, value: string) => data.set(type, value),
      getData: (type: string) => data.get(type) ?? "",
      get types() {
        return [...data.keys()];
      },
      effectAllowed: "all",
    };
    fireEvent.dragStart(screen.getByRole("tab", { name: "Files" }), { dataTransfer });
    expect(data.get(PANE_DRAG_TYPE)).toBe("files");
    fireEvent.dragOver(chatZone, { dataTransfer });
    fireEvent.drop(chatZone, { dataTransfer });
    expect(zoneOf(store.layout().root, "files")?.tabs).toContain("chat");
  });

  it("presets and the interface mode from the toolbar; Simple hides terminal, files, review", () => {
    const { store } = setup();
    fireEvent.change(screen.getByLabelText("Layout"), { target: { value: "quad" } });
    expect(store.getSnapshot().preset).toBe("quad");
    const advanced = store.layout();
    fireEvent.click(screen.getByRole("radio", { name: "Simple" }));
    expect(screen.queryByRole("tab", { name: "Terminal" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Open Terminal" })).toBeNull();
    fireEvent.click(screen.getByRole("radio", { name: "Advanced" }));
    expect(store.layout()).toBe(advanced);
  });

  it("arrow keys move between tabs of a zone", () => {
    const { store } = setup();
    const preview = screen.getByRole("tab", { name: "Preview" });
    fireEvent.keyDown(preview, { key: "ArrowRight" });
    expect(zoneOf(store.layout().root, "files")?.active).toBe("files");
  });

  it("has no axe violations", async () => {
    const { view } = setup();
    const results = await axe.run(view.container, {
      rules: { "color-contrast": { enabled: false } },
    });
    expect(results.violations.map((v) => `${v.id}: ${v.help}`)).toEqual([]);
  });
});

describe("drop geometry and slot numbers", () => {
  it("the middle is the centre, else the nearest edge", () => {
    const rect = { left: 0, top: 0, width: 100, height: 100 };
    expect(edgeAt(rect, { x: 50, y: 50 })).toBe("center");
    expect(edgeAt(rect, { x: 5, y: 50 })).toBe("left");
    expect(edgeAt(rect, { x: 95, y: 40 })).toBe("right");
    expect(edgeAt(rect, { x: 50, y: 3 })).toBe("top");
    expect(edgeAt(rect, { x: 50, y: 97 })).toBe("bottom");
    expect(edgeAt({ left: 0, top: 0, width: 0, height: 0 }, { x: 1, y: 1 })).toBe("center");
  });

  it("tabs 1–8 by position, 9 is the last", () => {
    expect([0, 7, 8, 9].map((i) => slotLabel(i, 10))).toEqual([1, 8, null, 9]);
    expect(slotLabel(2, 3)).toBe(3);
  });
});

describe("shortcuts hook", () => {
  function Probe(props: { onAction: (kind: string) => boolean; timers: ShortcutTimers }) {
    const slots = usePaneShortcuts(
      "linux",
      {
        onAction: (action) => props.onAction(action.kind),
      },
      props.timers,
    );
    return <output data-testid="slots">{slots ? "slots" : "none"}</output>;
  }

  it("holding the modifier 400 ms shows slot numbers; another key hides them", () => {
    const queue: Array<{ fn: () => void; ms: number }> = [];
    const timers: ShortcutTimers = {
      setTimeout: (fn, ms) => {
        queue.push({ fn, ms });
        return queue.length;
      },
      clearTimeout: () => undefined,
    };
    const onAction = vi.fn(() => true);
    render(<Probe onAction={onAction} timers={timers} />);
    fireEvent.keyDown(window, { key: "Control", ctrlKey: true });
    expect(queue[0]?.ms).toBe(400);
    act(() => queue[0]?.fn());
    expect(screen.getByTestId("slots").textContent).toBe("slots");
    fireEvent.keyDown(window, { key: "2", code: "Digit2", ctrlKey: true });
    expect(onAction).toHaveBeenCalledWith("jump");
    expect(screen.getByTestId("slots").textContent).toBe("none");
    fireEvent.keyUp(window, { key: "Control" });
  });
});
