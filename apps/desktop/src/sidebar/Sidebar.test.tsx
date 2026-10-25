// @vitest-environment jsdom
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import axe from "axe-core";
import { afterEach, describe, expect, it, vi } from "vitest";
import { translate } from "../i18n/index.tsx";
import { newChatTarget } from "./projects.ts";
import { type EventSource, relativeTime, Sidebar, SidebarModel, sessionDot } from "./Sidebar.tsx";
import type { ApiRequest, SessionInfo } from "./sessionList.ts";

class MemoryStorage {
  readonly data = new Map<string, string>();
  get length(): number {
    return this.data.size;
  }
  key(index: number): string | null {
    return [...this.data.keys()][index] ?? null;
  }
  getItem(key: string): string | null {
    return this.data.get(key) ?? null;
  }
  setItem(key: string, value: string): void {
    this.data.set(key, value);
  }
  removeItem(key: string): void {
    this.data.delete(key);
  }
}

const NOW = Date.now() / 1000;

const SESSIONS: SessionInfo[] = [
  {
    id: "s-tip",
    lineage_root: "s-root",
    title: "Parser work",
    cwd: "/work/indus",
    last_active: NOW - 60,
    message_count: 4,
  },
  { id: "s-pinned", title: "Pinned chat", pinned: true, last_active: NOW - 120, message_count: 2 },
  { id: "s-plain", title: "Lunch", last_active: NOW - 7200, message_count: 2 },
  {
    id: "s-branch",
    title: "Try another way",
    kind: "branch",
    parent_session_id: "s-plain",
    last_active: NOW - 30,
  },
];

function fakeApi(sessions: SessionInfo[] = SESSIONS) {
  return vi.fn<ApiRequest>(async (request) => {
    if (request.method === "PATCH") return { kind: "response", status: 200, body: {} };
    if (request.path.startsWith("/api/sessions/search")) {
      return {
        kind: "response",
        status: 200,
        body: { results: [{ session_id: "s-far", title: "Far away", snippet: "far" }] },
      };
    }
    return { kind: "response", status: 200, body: { sessions, total: sessions.length } };
  });
}

function mount(
  options: {
    api?: ReturnType<typeof fakeApi>;
    events?: EventSource | null;
    active?: string | null;
  } = {},
) {
  const storage = new MemoryStorage();
  const api = options.api ?? fakeApi();
  const model = new SidebarModel({ api, storage, listDir: null });
  const onOpenSession = vi.fn();
  const onNewChat = vi.fn();
  const view = render(
    <Sidebar
      model={model}
      profile="work"
      activeSessionId={options.active ?? null}
      onOpenSession={onOpenSession}
      onNewChat={onNewChat}
      events={options.events ?? null}
    />,
  );
  return { view, model, storage, api, onOpenSession, onNewChat };
}

afterEach(() => {
  vi.useRealTimers();
});

describe("sessions sidebar", () => {
  it("lists the profile's sessions: pinned on top, the branch as its own row", async () => {
    const { api } = mount();
    const pinned = await screen.findByRole("list", { name: "Pinned" });
    expect(within(pinned).getByText("Pinned chat")).toBeTruthy();
    const chats = screen.getByRole("list", { name: "Sessions" });
    expect(
      within(chats).getAllByRole("button", { name: /^(Try another way|Parser work|Lunch)/ }),
    ).toHaveLength(3);
    expect(within(chats).getByText("(branch)")).toBeTruthy();
    expect(api.mock.calls[0]?.[0].profile).toBe("work");
  });

  it("W54-D1-2: the row's ⋮ button opens its menu; Branch shows only when it can be backed", async () => {
    mount();
    fireEvent.click(await screen.findByRole("button", { name: "More actions for Parser work" }));
    expect(screen.getByRole("menuitem", { name: "Pin" })).toBeTruthy();
    expect(screen.queryByRole("menuitem", { name: "Branch" })).toBeNull();
  });

  it("W54-D1-8: Branch asks the host with the row's newest id", async () => {
    const storage = new MemoryStorage();
    const model = new SidebarModel({ api: fakeApi(), storage, listDir: null });
    const onBranch = vi.fn();
    render(
      <Sidebar
        model={model}
        profile="work"
        activeSessionId={null}
        onOpenSession={vi.fn()}
        onNewChat={vi.fn()}
        onBranch={onBranch}
      />,
    );
    fireEvent.contextMenu(await screen.findByRole("button", { name: /^Parser work/ }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Branch" }));
    expect(onBranch).toHaveBeenCalledWith("s-tip");
  });

  it("W58-J1-5: Open in terminal shows on a row with a folder and asks the host", async () => {
    const storage = new MemoryStorage();
    const model = new SidebarModel({ api: fakeApi(), storage, listDir: null });
    const onOpenInTerminal = vi.fn();
    render(
      <Sidebar
        model={model}
        profile="work"
        activeSessionId={null}
        onOpenSession={vi.fn()}
        onNewChat={vi.fn()}
        onOpenInTerminal={onOpenInTerminal}
      />,
    );
    // a row without a folder has no such item
    fireEvent.contextMenu(await screen.findByRole("button", { name: /^Lunch/ }));
    expect(screen.queryByRole("menuitem", { name: "Open in terminal" })).toBeNull();
    fireEvent.keyDown(screen.getByRole("menu"), { key: "Escape" });
    fireEvent.contextMenu(screen.getByRole("button", { name: /^Parser work/ }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Open in terminal" }));
    expect(onOpenInTerminal).toHaveBeenCalledWith("s-tip");
  });

  it("W58-J1-5: no Open in terminal without a host that can open the pane", async () => {
    mount();
    fireEvent.contextMenu(await screen.findByRole("button", { name: /^Parser work/ }));
    expect(screen.queryByRole("menuitem", { name: "Open in terminal" })).toBeNull();
  });

  it("W54-D1-2: dots rank waiting > working > new activity > live > quiet", () => {
    expect(sessionDot({ attention: true, running: true, unread: true, live: true })).toBe(
      "attention",
    );
    expect(sessionDot({ attention: false, running: true, unread: true })).toBe("running");
    expect(sessionDot({ attention: false, running: false, unread: true, live: true })).toBe(
      "unread",
    );
    expect(sessionDot({ attention: false, running: false, unread: false, live: true })).toBe(
      "live",
    );
    expect(sessionDot({ attention: false, running: false, unread: false })).toBe("idle");
  });

  it("opens a conversation by its newest id", async () => {
    const { onOpenSession } = mount();
    fireEvent.click(await screen.findByRole("button", { name: /^Parser work/ }));
    expect(onOpenSession).toHaveBeenCalledWith("s-tip");
  });

  it("pins on the durable id (row menu)", async () => {
    const { api } = mount();
    fireEvent.contextMenu(await screen.findByRole("button", { name: /^Parser work/ }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Pin" }));
    await waitFor(() =>
      expect(api.mock.calls.at(-1)?.[0]).toMatchObject({
        method: "PATCH",
        path: "/api/sessions/s-root",
        body: { pinned: true },
      }),
    );
    const pinned = screen.getByRole("list", { name: "Pinned" });
    expect(within(pinned).getByText("Parser work")).toBeTruthy();
  });

  it("shift-click pins without opening the chat (W52-h)", async () => {
    const { api, onOpenSession } = mount();
    fireEvent.click(await screen.findByRole("button", { name: /^Lunch/ }), { shiftKey: true });
    expect(onOpenSession).not.toHaveBeenCalled();
    await waitFor(() =>
      expect(api.mock.calls.at(-1)?.[0]).toMatchObject({
        method: "PATCH",
        path: "/api/sessions/s-plain",
        body: { pinned: true },
      }),
    );
  });

  it("the row menu renames inline and deletes after a confirmation (W52-h)", async () => {
    const { api } = mount();
    const row = await screen.findByRole("button", { name: /^Lunch/ });
    fireEvent.keyDown(row, { key: "F10", shiftKey: true });
    fireEvent.click(screen.getByRole("menuitem", { name: "Rename…" }));
    const field = screen.getByRole("textbox", { name: "Session title" });
    fireEvent.change(field, { target: { value: "Lunch plans" } });
    fireEvent.keyDown(field, { key: "Enter" });
    await waitFor(() =>
      expect(api.mock.calls.at(-1)?.[0]).toMatchObject({
        method: "PATCH",
        path: "/api/sessions/s-plain",
        body: { title: "Lunch plans" },
      }),
    );
    fireEvent.contextMenu(await screen.findByRole("button", { name: /^Lunch plans/ }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Delete" }));
    const dialog = screen.getByRole("dialog", { name: "Delete this session?" });
    await act(async () => {
      fireEvent.click(within(dialog).getByRole("button", { name: "Delete" }));
    });
    await waitFor(() => {
      const call = api.mock.calls.at(-1)?.[0];
      expect(call?.method).toBe("DELETE");
      expect(call?.path.startsWith("/api/sessions/s-plain?")).toBe(true);
    });
    expect(screen.queryByRole("button", { name: /^Lunch plans/ })).toBeNull();
  });

  it("search finds a pasted id at once and adds the server's hits", async () => {
    mount();
    await screen.findByRole("button", { name: /^Lunch/ });
    fireEvent.change(screen.getByRole("searchbox", { name: "Search conversations" }), {
      target: { value: " s-plain " },
    });
    const results = screen.getByRole("list", { name: "Results" });
    expect(within(results).getByRole("button", { name: /^Lunch/ })).toBeTruthy();
    expect(await screen.findByRole("button", { name: /^Far away/ })).toBeTruthy();
    fireEvent.keyDown(screen.getByRole("searchbox"), { key: "Escape" });
    expect(screen.queryByRole("list", { name: "Results" })).toBeNull();
  });

  it("a search with no match says so, with one clear button (W51-b)", async () => {
    const base = fakeApi();
    const api = vi.fn<ApiRequest>(async (request) =>
      request.path.startsWith("/api/sessions/search")
        ? { kind: "response", status: 200, body: { results: [] } }
        : base(request),
    );
    mount({ api });
    await screen.findByRole("button", { name: /^Lunch/ });
    fireEvent.change(screen.getByRole("searchbox", { name: "Search conversations" }), {
      target: { value: "zzqx storyboard" },
    });
    expect(await screen.findByText("No sessions match \u201czzqx storyboard\u201d.")).toBeTruthy();
    expect(screen.getAllByRole("button", { name: "Clear search" })).toHaveLength(1);
  });

  it("a project groups its sessions and its new chat starts in its folder", async () => {
    const { model, storage, onNewChat } = mount();
    await screen.findByRole("button", { name: /^Lunch/ });
    act(() => {
      model.projects("work").add("Indus", ["/work/indus"]);
    });
    const group = screen.getByRole("list", { name: "Indus" });
    expect(within(group).getByText("Parser work")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "New chat in Indus" }));
    expect(newChatTarget.get(storage, "work")).toBe("/work/indus");
    expect(onNewChat).toHaveBeenCalled();
  });

  it("creates a project from the form", async () => {
    const { model } = mount();
    fireEvent.click(screen.getByRole("button", { name: "New project" }));
    const form = screen.getByRole("form", { name: "New project" });
    fireEvent.change(within(form).getByLabelText("Name"), { target: { value: "Docs" } });
    fireEvent.change(within(form).getByLabelText("Folders"), { target: { value: "/docs\n/more" } });
    fireEvent.click(within(form).getByRole("button", { name: "Create project" }));
    expect(model.projects("work").getPrefs().projects[0]).toMatchObject({
      name: "Docs",
      folders: ["/docs", "/more"],
    });
  });

  it("unread survives a remount; the open conversation is marked seen", async () => {
    const storage = new MemoryStorage();
    const first = new SidebarModel({ api: fakeApi(), storage, listDir: null });
    const view = render(
      <Sidebar
        model={first}
        profile="work"
        activeSessionId={null}
        onOpenSession={() => undefined}
        onNewChat={() => undefined}
      />,
    );
    await screen.findByRole("button", { name: /^Lunch/ });
    view.unmount();
    const grown = SESSIONS.map((s) => (s.id === "s-plain" ? { ...s, message_count: 6 } : s));
    const second = new SidebarModel({ api: fakeApi(grown), storage, listDir: null });
    const again = render(
      <Sidebar
        model={second}
        profile="work"
        activeSessionId={null}
        onOpenSession={() => undefined}
        onNewChat={() => undefined}
      />,
    );
    const lunch = await screen.findByRole("button", { name: /^Lunch/ });
    expect(within(lunch).getByText("(new activity)")).toBeTruthy();
    again.rerender(
      <Sidebar
        model={second}
        profile="work"
        activeSessionId="s-plain"
        onOpenSession={() => undefined}
        onNewChat={() => undefined}
      />,
    );
    await waitFor(() =>
      expect(
        within(screen.getByRole("button", { name: /^Lunch/ })).queryByText("(new activity)"),
      ).toBeNull(),
    );
  });

  it("a running turn shows the working dot", async () => {
    let emit: (event: { type: string; session_id: string }) => void = () => undefined;
    const events: EventSource = {
      onEvent: (listener) => {
        emit = listener;
        return () => undefined;
      },
    };
    mount({ events });
    await screen.findByRole("button", { name: /^Lunch/ });
    act(() => emit({ type: "turn.started", session_id: "s-plain" }));
    expect(
      within(screen.getByRole("button", { name: /^Lunch/ })).getByText("(working)"),
    ).toBeTruthy();
  });

  it("the archived view lists archived rows; restoring one PATCHes it back", async () => {
    const api = vi.fn<ApiRequest>(async (request) => {
      if (request.method === "PATCH") return { kind: "response", status: 200, body: {} };
      const archived = request.path.includes("archived=only");
      return {
        kind: "response",
        status: 200,
        body: {
          sessions: archived
            ? [{ id: "old", title: "Old one", archived: true, last_active: NOW }]
            : [],
        },
      };
    });
    mount({ api });
    fireEvent.click(screen.getByRole("button", { name: "Filter sessions" }));
    fireEvent.click(screen.getByRole("menuitemradio", { name: "Archived" }));
    fireEvent.contextMenu(await screen.findByRole("button", { name: /^Old one/ }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Unarchive" }));
    await waitFor(() =>
      expect(api.mock.calls.at(-1)?.[0]).toMatchObject({
        path: "/api/sessions/old",
        body: { archived: false },
      }),
    );
  });

  it("a failed load says so and retries", async () => {
    let fail = true;
    const api = vi.fn<ApiRequest>(async () =>
      fail
        ? { kind: "response", status: 503, body: {} }
        : { kind: "response", status: 200, body: { sessions: SESSIONS } },
    );
    mount({ api });
    expect((await screen.findByRole("alert")).textContent).toContain("could not be loaded");
    fail = false;
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("button", { name: /^Lunch/ })).toBeTruthy();
  });

  it("has no axe violations", async () => {
    const { view } = mount();
    await screen.findByRole("button", { name: /^Lunch/ });
    const results = await axe.run(view.container, {
      rules: { "color-contrast": { enabled: false } },
    });
    expect(results.violations.map((v) => `${v.id}: ${v.help}`)).toEqual([]);
  });

  it("relative times", () => {
    const now = 1_000_000_000_000;
    expect(relativeTime(translate, now - 10_000, now)).toBe("now");
    expect(relativeTime(translate, now - 5 * 60_000, now)).toBe("5m");
    expect(relativeTime(translate, now - 3 * 3_600_000, now)).toBe("3h");
    expect(relativeTime(translate, now - 50 * 3_600_000, now)).toBe("2d");
    expect(relativeTime(translate, 0, now)).toBe("");
  });
});
