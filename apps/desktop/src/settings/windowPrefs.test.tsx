// @vitest-environment jsdom
// The Window & layout rows made real in W56-G1: App actions (the band cluster's side), File
// browser (Files shown when a workspace arrives), Floating composer (the dock's grip), and the
// session row menu's Export.
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChatView } from "../chat/ChatView.tsx";
import { ComposerDock, clampPlace, LIFT, NUDGE } from "../chat/ComposerDock.tsx";
import { TranscriptStoreProvider } from "../chat/transcriptReact.tsx";
import { type TranscriptSource, TranscriptStore } from "../chat/transcriptStore.ts";
import { LayoutStore, type StorageLike } from "../panes/layoutStore.ts";
import { PaneRegistry, PaneRegistryProvider, PaneShell } from "../panes/PaneShell.tsx";
import { allPanes, zoneOf } from "../panes/paneTree.ts";
import { SessionRowMenu } from "../sidebar/SessionRowMenu.tsx";
import { DISPLAY_DEFAULTS, publishDisplayPrefs, resetDisplayPrefs } from "./displayPrefs.ts";

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

function shell(options: { preset?: boolean } = {}) {
  const registry = new PaneRegistry();
  registry.register({ id: "files", stateful: false, render: () => <p>file tree</p> });
  registry.register({ id: "review", stateful: false, render: () => <p>diffs</p> });
  registry.register({ id: "preview", stateful: true, render: () => <p>page</p> });
  const store = new LayoutStore({ storage: memory(), available: () => registry.ids() });
  if (options.preset === true) store.applyPreset("default");
  const ui = (workspace: string | null) => (
    <PaneRegistryProvider registry={registry}>
      <PaneShell
        store={store}
        chat={<p>the chat</p>}
        sessionId="s1"
        profile="default"
        workspace={workspace}
      />
    </PaneRegistryProvider>
  );
  const view = render(ui(null));
  return { store, view, show: (workspace: string | null) => view.rerender(ui(workspace)) };
}

const filesShowing = (store: LayoutStore) =>
  zoneOf(store.layout().root, "files")?.active === "files";

beforeEach(() => resetDisplayPrefs());
afterEach(() => {
  cleanup();
  resetDisplayPrefs();
  vi.restoreAllMocks();
});

describe("App actions (W56-G1)", () => {
  it("right by default; left moves the cluster and marks the top-left zone for its tabs", () => {
    const { view } = shell({ preset: true });
    const host = view.container.querySelector(".pane-shell") as HTMLElement;
    expect(host.dataset.appActions).toBeUndefined();
    expect(view.container.querySelector("[data-band-start]")).toBeNull();
    expect(view.container.querySelector("[data-band-end]")).not.toBeNull();
    act(() => publishDisplayPrefs({ ...DISPLAY_DEFAULTS, appActionsSide: "left" }));
    expect(host.dataset.appActions).toBe("left");
    const start = view.container.querySelector("[data-band-start='true']") as HTMLElement;
    expect(start).not.toBeNull();
    // the left-most top zone holds the chat in the default preset
    expect(start.textContent).toContain("the chat");
    expect(view.container.querySelector("[data-band-end]")).toBeNull();
    // the top-right zone keeps clear of the window controls
    expect(view.container.querySelector("[data-band-edge='true']")).not.toBeNull();
    expect(host.style.getPropertyValue("--band-shift")).toBe("");
    act(() => publishDisplayPrefs({ ...DISPLAY_DEFAULTS, appActionsSide: "right" }));
    expect(host.dataset.appActions).toBeUndefined();
    expect(view.container.querySelector("[data-band-start]")).toBeNull();
  });
});

describe("File browser (W56-G1)", () => {
  it("off by default: a workspace leaves the layout alone", () => {
    const { store, show } = shell();
    show("/home/me/repo");
    expect(allPanes(store.layout().root)).not.toContain("files");
  });

  it("on: each new workspace shows Files once; closing it lasts until the folder changes", () => {
    const { store, show } = shell();
    act(() => publishDisplayPrefs({ ...DISPLAY_DEFAULTS, fileBrowser: true }));
    expect(allPanes(store.layout().root)).not.toContain("files"); // no workspace yet
    show("/home/me/repo");
    expect(filesShowing(store)).toBe(true);
    act(() => store.close("files"));
    show("/home/me/repo");
    expect(allPanes(store.layout().root)).not.toContain("files");
    show("/home/me/other");
    expect(filesShowing(store)).toBe(true);
  });

  it("turning it on with a workspace set shows Files at once; Simple mode never does", () => {
    const { store, show } = shell();
    show("/home/me/repo");
    act(() => publishDisplayPrefs({ ...DISPLAY_DEFAULTS, fileBrowser: true }));
    expect(filesShowing(store)).toBe(true);
    act(() => store.close("files"));
    act(() => store.setMode("simple"));
    show("/home/me/elsewhere");
    expect(allPanes(store.layout().root)).not.toContain("files");
  });
});

// -- the floating composer -------------------------------------------------------------------------

/** jsdom has no layout: the chat column is 800×600 and the dock sits at its bottom. */
function fakeBoxes() {
  vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
    const el = this as HTMLElement;
    if (el.classList.contains("column")) return new DOMRect(0, 0, 800, 600);
    if (el.classList.contains("chat-composer")) {
      if (el.dataset.floating === "true") {
        return new DOMRect(
          Number.parseFloat(el.style.left) || 0,
          Number.parseFloat(el.style.top) || 0,
          640,
          80,
        );
      }
      return new DOMRect(16, 510, 768, 80);
    }
    return new DOMRect(0, 0, 0, 0);
  });
}

function dock(floatable: boolean) {
  return (
    <section className="column">
      <ComposerDock floatable={floatable}>
        <textarea aria-label="Message" />
      </ComposerDock>
    </section>
  );
}

const card = () => document.querySelector(".chat-composer") as HTMLElement;

describe("Floating composer (W56-G1)", () => {
  it("off: no grip, the composer stays docked", () => {
    render(dock(false));
    expect(screen.queryByRole("button", { name: /Float the message box/ })).toBeNull();
    expect(card().dataset.floating).toBeUndefined();
  });

  it("the grip floats the composer above its dock and docks it again", () => {
    fakeBoxes();
    render(dock(true));
    const grip = screen.getByRole("button", { name: /Float the message box/ });
    expect(grip.getAttribute("aria-pressed")).toBe("false");
    fireEvent.click(grip);
    expect(card().dataset.floating).toBe("true");
    expect(card().style.top).toBe(`${510 - LIFT}px`);
    expect(card().style.left).toBe("16px");
    const docker = screen.getByRole("button", { name: /Dock the message box/ });
    expect(docker.getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(docker);
    expect(card().dataset.floating).toBeUndefined();
    expect(card().style.top).toBe("");
  });

  it("a drag pulls it out of the dock and moves it; the click that ends a drag does not dock", () => {
    fakeBoxes();
    render(dock(true));
    const grip = screen.getByRole("button", { name: /Float the message box/ });
    fireEvent.pointerDown(grip, { button: 0, clientX: 100, clientY: 520, pointerId: 1 });
    fireEvent.pointerMove(grip, { clientX: 101, clientY: 519, pointerId: 1 }); // under the threshold
    expect(card().dataset.floating).toBeUndefined();
    fireEvent.pointerMove(grip, { clientX: 140, clientY: 300, pointerId: 1 });
    expect(card().dataset.floating).toBe("true");
    expect(card().style.left).toBe("56px");
    expect(card().style.top).toBe("290px");
    fireEvent.pointerUp(grip, { clientX: 140, clientY: 300, pointerId: 1 });
    fireEvent.click(grip);
    expect(card().dataset.floating).toBe("true");
    // dragged far right and down: kept inside the column (800 - 640, 600 - 80)
    fireEvent.pointerDown(grip, { button: 0, clientX: 140, clientY: 300, pointerId: 2 });
    fireEvent.pointerMove(grip, { clientX: 2000, clientY: 2000, pointerId: 2 });
    fireEvent.pointerUp(grip, { pointerId: 2 });
    expect(card().style.left).toBe("160px");
    expect(card().style.top).toBe("520px");
  });

  it("arrow keys move a floating composer; turning the setting off docks it", () => {
    fakeBoxes();
    const view = render(dock(true));
    const grip = screen.getByRole("button", { name: /Float the message box/ });
    fireEvent.keyDown(grip, { key: "ArrowRight" }); // docked: nothing to move
    expect(card().dataset.floating).toBeUndefined();
    fireEvent.click(grip);
    fireEvent.keyDown(grip, { key: "ArrowRight" });
    fireEvent.keyDown(grip, { key: "ArrowUp" });
    expect(card().style.left).toBe(`${16 + NUDGE}px`);
    expect(card().style.top).toBe(`${510 - LIFT - NUDGE}px`);
    view.rerender(dock(false));
    expect(card().dataset.floating).toBeUndefined();
    expect(screen.queryByRole("button", { name: /message box/ })).toBeNull();
  });

  it("clampPlace keeps the card inside, top-left first when it cannot fit", () => {
    const area = { width: 800, height: 600 };
    const box = { width: 640, height: 80 };
    expect(clampPlace({ x: -20, y: -5 }, box, area)).toEqual({ x: 0, y: 0 });
    expect(clampPlace({ x: 500, y: 900 }, box, area)).toEqual({ x: 160, y: 520 });
    expect(clampPlace({ x: 50, y: 50 }, { width: 900, height: 700 }, area)).toEqual({
      x: 0,
      y: 0,
    });
  });

  it("only the main window's chat view offers the grip (the HUD keeps its own composer)", () => {
    const source: TranscriptSource = {
      history: async () => ({ messages: [], total: 0 }),
      onEvent: () => () => undefined,
    } as unknown as TranscriptSource;
    const store = new TranscriptStore(source);
    const view = (floatable: boolean) => (
      <MemoryRouter>
        <TranscriptStoreProvider store={store}>
          <ChatView
            sessionId={null}
            composer={<textarea aria-label="Message" />}
            {...(floatable ? { floatableComposer: true } : {})}
          />
        </TranscriptStoreProvider>
      </MemoryRouter>
    );
    const shown = render(view(true));
    expect(screen.queryByRole("button", { name: /Float the message box/ })).toBeNull(); // pref off
    act(() => publishDisplayPrefs({ ...DISPLAY_DEFAULTS, floatingComposer: true }));
    expect(screen.getByRole("button", { name: /Float the message box/ })).toBeTruthy();
    shown.rerender(view(false));
    expect(screen.queryByRole("button", { name: /Float the message box/ })).toBeNull();
  });
});

// -- Export in the row menu ------------------------------------------------------------------------

describe("session row menu Export (W56-G1)", () => {
  const base = {
    rename: () => undefined,
    togglePin: () => undefined,
    toggleUnread: () => undefined,
    copyId: () => undefined,
    toggleArchive: () => undefined,
    remove: () => undefined,
  };

  it("offers Export when the window can export, and runs it after closing the menu", () => {
    const exportFile = vi.fn();
    const onClose = vi.fn();
    render(
      <SessionRowMenu
        x={10}
        y={10}
        title="Plan"
        pinned={false}
        unread={false}
        archived={false}
        actions={{ ...base, exportFile }}
        onClose={onClose}
      />,
    );
    fireEvent.click(screen.getByRole("menuitem", { name: /Export/ }));
    expect(onClose).toHaveBeenCalled();
    expect(exportFile).toHaveBeenCalledTimes(1);
  });

  it("no Export item without an exporter", () => {
    render(
      <SessionRowMenu
        x={10}
        y={10}
        title="Plan"
        pinned={false}
        unread={false}
        archived={false}
        actions={base}
        onClose={() => undefined}
      />,
    );
    expect(screen.queryByRole("menuitem", { name: /Export/ })).toBeNull();
  });
});
