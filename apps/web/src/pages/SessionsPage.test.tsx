import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createMockApi, type MockApi } from "../testing/mockApi.ts";
import { currentLocation, renderPage } from "../testing/render.tsx";
import { SessionsPage } from "./SessionsPage.tsx";
import { AUTOMATION_SOURCES, CONFLICT_MESSAGES } from "./sessions/sources.ts";

let mock: MockApi;
let restore: () => void;

beforeEach(() => {
  mock = createMockApi();
  restore = mock.install();
  vi.spyOn(console, "warn").mockImplementation(() => {});
});

afterEach(() => {
  restore();
});

async function row(id: string) {
  return within(await screen.findByTestId(`session-${id}`));
}

/** The page opens on History (the rows with actions); the click keeps it there. */
function renderHistory(path = "/sessions") {
  renderPage(<SessionsPage />, path);
  fireEvent.click(screen.getByRole("button", { name: "History" }));
}

describe("delete", () => {
  for (const [code, message] of Object.entries(CONFLICT_MESSAGES)) {
    it(`explains a 409 ${code} in plain language`, async () => {
      mock.state.deleteConflicts["s-chat-1"] = code;
      renderHistory();
      fireEvent.click((await row("s-chat-1")).getByRole("button", { name: "Delete" }));
      const dialog = await screen.findByRole("dialog");
      fireEvent.click(within(dialog).getByRole("button", { name: "Delete" }));
      expect((await within(dialog).findByRole("alert")).textContent).toBe(message);
      expect(screen.getByTestId("session-s-chat-1")).toBeTruthy();
    });
  }

  it("sends the snapshot identity and removes the row", async () => {
    renderHistory();
    fireEvent.click((await row("s-chat-2")).getByRole("button", { name: "Delete" }));
    fireEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Delete" }),
    );
    await waitFor(() => expect(screen.queryByTestId("session-s-chat-2")).toBeNull());
    expect(mock.callsTo("GET", "/api/sessions/s-chat-2/mutation-snapshot")).toHaveLength(1);
    const del = mock.callsTo("DELETE", "/api/sessions/s-chat-2")[0];
    expect(del?.query.get("expected_revision")).toBe("7");
    expect(del?.query.get("request_id")).toBeTruthy();
  });
});

describe("prune", () => {
  it("shows the dry-run count before pruning", async () => {
    renderHistory();
    fireEvent.click(await screen.findByRole("button", { name: "Prune old sessions" }));
    const dialog = await screen.findByRole("dialog");
    const pruneButton = () =>
      within(dialog).getByRole("button", { name: "Prune" }) as HTMLButtonElement;
    expect(within(dialog).queryByRole("button", { name: "Check" })).toBeNull();

    fireEvent.change(within(dialog).getByLabelText("Older than (days)"), {
      target: { value: "30" },
    });
    expect(pruneButton().disabled).toBe(true);
    await waitFor(() =>
      expect(within(dialog).getByTestId("prune-preview").textContent).toContain(
        "3 sessions would be removed",
      ),
    );
    const dry = mock.callsTo("POST", "/api/sessions/prune");
    expect(dry.at(-1)?.body).toEqual({ older_than_days: 30, dry_run: true });
    expect(dry.every((call) => (call.body as { dry_run: boolean }).dry_run)).toBe(true);

    await waitFor(() => expect(pruneButton().disabled).toBe(false));
    fireEvent.click(pruneButton());
    expect(await screen.findByText("Removed 3 sessions.")).toBeTruthy();
    expect(mock.callsTo("POST", "/api/sessions/prune").at(-1)?.body).toEqual({
      older_than_days: 30,
      dry_run: false,
    });
  });

  it("refuses a day count that is not a whole number", async () => {
    renderHistory();
    fireEvent.click(await screen.findByRole("button", { name: "Prune old sessions" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText("Older than (days)"), {
      target: { value: "-4" },
    });
    expect(within(dialog).getByText("Enter a whole number of days.")).toBeTruthy();
    expect(
      (within(dialog).getByRole("button", { name: "Prune" }) as HTMLButtonElement).disabled,
    ).toBe(true);
  });
});

describe("browse", () => {
  it("resumes a session in Chat with its id", async () => {
    renderHistory("/sessions?profile=work");
    fireEvent.click((await row("s-chat-1")).getByRole("button", { name: /Resume/ }));
    await waitFor(() => expect(currentLocation()).toBe("/chat?resume=s-chat-1&profile=work"));
  });

  it("filters by tab: chats exclude automation, automation includes only it", async () => {
    renderHistory();
    await row("s-chat-1");
    expect(screen.queryByTestId("session-s-cron-1")).toBeNull();
    expect(mock.callsTo("GET", "/api/sessions")[0]?.query.get("exclude_sources")).toBe(
      AUTOMATION_SOURCES.join(","),
    );

    fireEvent.click(screen.getByRole("button", { name: "Automation" }));
    await row("s-cron-1");
    expect(screen.queryByTestId("session-s-chat-1")).toBeNull();
    expect(mock.callsTo("GET", "/api/sessions").at(-1)?.query.get("sources")).toBe(
      AUTOMATION_SOURCES.join(","),
    );

    fireEvent.click(screen.getByRole("button", { name: "All" }));
    await row("s-chat-1");
    await row("s-cron-1");
  });

  it("filters by one or several sources from the source menu", async () => {
    renderHistory();
    await row("s-chat-1");
    // Stats load before the menu lists anything.
    await waitFor(() =>
      expect(mock.callsTo("GET", "/api/sessions/stats").length).toBeGreaterThan(0),
    );
    fireEvent.click(screen.getByRole("button", { name: "Source" }));
    const menu = await screen.findByRole("group", { name: "Chat sources" });
    fireEvent.click(await within(menu).findByRole("checkbox", { name: /WEB/ }));
    await waitFor(() => expect(screen.queryByTestId("session-s-chat-1")).toBeNull());
    expect(mock.callsTo("GET", "/api/sessions").at(-1)?.query.get("source")).toBe("web");
    expect(screen.getByRole("button", { name: "Source" }).textContent).toContain("WEB");

    fireEvent.click(within(menu).getByRole("checkbox", { name: /CLI/ }));
    await row("s-chat-1");
    expect(mock.callsTo("GET", "/api/sessions").at(-1)?.query.get("sources")).toBe("web,cli");
    expect(screen.getByRole("button", { name: "Source" }).textContent).toContain("2 sources");

    fireEvent.click(within(menu).getByRole("button", { name: "Clear" }));
    await waitFor(() =>
      expect(mock.callsTo("GET", "/api/sessions").at(-1)?.query.get("exclude_sources")).toBe(
        AUTOMATION_SOURCES.join(","),
      ),
    );
  });

  it("deletes the selected sessions one by one", async () => {
    renderHistory();
    fireEvent.click((await row("s-chat-1")).getByRole("checkbox"));
    fireEvent.click((await row("s-chat-2")).getByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: "Delete 2 selected" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(screen.queryByTestId("session-s-chat-1")).toBeNull());
    await waitFor(() => expect(screen.queryByTestId("session-s-chat-2")).toBeNull());
    expect(mock.callsTo("DELETE", "/api/sessions/s-chat-1")).toHaveLength(1);
    expect(mock.callsTo("DELETE", "/api/sessions/s-chat-2")).toHaveLength(1);
  });

  it("renders search snippets with marks and scrolls the opened hit to its first match", async () => {
    const scrolled = vi.fn();
    Element.prototype.scrollIntoView = scrolled;
    renderHistory();
    fireEvent.change(await screen.findByLabelText("Search sessions"), {
      target: { value: "parser" },
    });
    // Search runs as you type (after a short pause) and the results replace the list.
    const results = await screen.findByRole("list", { name: "Search results" });
    expect(screen.queryByRole("list", { name: "Sessions" })).toBeNull();
    const mark = await within(results).findByText("parser", { selector: "mark" });
    expect(mark.tagName).toBe("MARK");
    // No HTML is injected: the snippet's surrounding text is plain text nodes.
    expect(results.innerHTML).not.toContain("&lt;mark&gt;");

    fireEvent.click(within(results).getByRole("button", { name: "Refactor the parser" }));
    const detail = await screen.findByTestId("detail-s-chat-1");
    expect(
      (await within(detail).findAllByText("parser", { selector: "mark" })).length,
    ).toBeGreaterThan(0);
    await waitFor(() => expect(scrolled).toHaveBeenCalled());
  });

  it("shows the history with tool calls folded", async () => {
    renderHistory();
    fireEvent.click((await row("s-chat-1")).getByRole("button", { name: "Refactor the parser" }));
    const detail = await screen.findByTestId("detail-s-chat-1");
    const call = await within(detail).findByTestId("tool-call-t1");
    expect(call.tagName).toBe("DETAILS");
    expect(call.hasAttribute("open")).toBe(false);
    expect(within(call).getByText("read_file")).toBeTruthy();
    expect(within(detail).getByText("Tool: read_file")).toBeTruthy();
    expect(within(detail).getByRole("heading", { name: "Done" })).toBeTruthy();
  });

  it("renames with the mutation identity", async () => {
    renderHistory();
    fireEvent.click((await row("s-chat-2")).getByRole("button", { name: "Rename" }));
    const input = screen.getByLabelText("Session title");
    fireEvent.change(input, { target: { value: "Release plan v2" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await screen.findByRole("button", { name: "Release plan v2" });
    const patch = mock.callsTo("PATCH", "/api/sessions/s-chat-2")[0]?.body as Record<
      string,
      unknown
    >;
    expect(patch.title).toBe("Release plan v2");
    expect(patch.expected_revision).toBe(7);
    expect(typeof patch.request_id).toBe("string");
  });

  it("shows the stats bar", async () => {
    renderHistory();
    const stats = await screen.findByLabelText("Session statistics");
    await waitFor(() =>
      expect(within(stats).getByText("Total").nextSibling?.textContent).toBe("3"),
    );
  });

  it("opens on History; the Overview's recent cards open a session back in History", async () => {
    renderPage(<SessionsPage />, "/sessions");
    expect(await screen.findByTestId("session-s-chat-1")).toBeTruthy();
    expect(screen.getByRole("button", { name: "History" }).getAttribute("aria-pressed")).toBe(
      "true",
    );
    expect(screen.getByLabelText("Search sessions")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Overview" }));
    const recent = await screen.findByRole("list", { name: "Recent sessions" });
    expect(screen.queryByTestId("session-s-chat-1")).toBeNull();
    fireEvent.click(within(recent).getByRole("button", { name: "Refactor the parser" }));
    expect(await screen.findByTestId("detail-s-chat-1")).toBeTruthy();
    expect(screen.getByRole("button", { name: "History" }).getAttribute("aria-pressed")).toBe(
      "true",
    );
  });

  it("shows the empty state for a filter with no sessions", async () => {
    mock.state.sessions = [];
    renderPage(<SessionsPage />, "/sessions");
    expect(await screen.findByText("No sessions yet")).toBeTruthy();
    // Nothing for the Overview: the view switch goes, the search box shows.
    expect(screen.queryByRole("button", { name: "History" })).toBeNull();
    expect(screen.getByLabelText("Search sessions")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Automation" }));
    expect(await screen.findByText("No sessions in this filter")).toBeTruthy();
  });
});

describe("import", () => {
  it("lists other tools' conversations and imports one into the history", async () => {
    mock.state.importCandidates = [
      {
        source: "codex",
        path: "/home/u/.codex/sessions/a.jsonl",
        title: "Fix the flaky test",
        cwd: "/work/app",
        modified: Date.now() / 1000 - 60,
        size: 2048,
      },
    ];
    renderHistory();
    const open = await screen.findByRole("button", { name: "Import sessions" });
    expect((open as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(open);
    const dialog = within(await screen.findByRole("dialog", { name: "Import sessions" }));
    expect(await dialog.findByText("Fix the flaky test")).toBeTruthy();
    expect(dialog.getByText("Codex")).toBeTruthy();
    fireEvent.click(dialog.getByRole("button", { name: "Import Fix the flaky test" }));
    expect(await dialog.findByText("Imported")).toBeTruthy();
    expect(mock.callsTo("POST", "/api/session-import/import")[0]?.body).toEqual({
      source: "codex",
      path: "/home/u/.codex/sessions/a.jsonl",
    });
    expect(await screen.findByText(/Imported “Fix the flaky test” \(4 messages\)/)).toBeTruthy();
  });

  it("says when a conversation was imported before, and when there is nothing to import", async () => {
    mock.state.importCandidates = [
      {
        source: "claude-code",
        path: "/home/u/.claude/projects/p/b.jsonl",
        title: "Old chat",
        cwd: null,
        modified: 0,
        size: 10,
      },
    ];
    mock.state.imported = ["/home/u/.claude/projects/p/b.jsonl"];
    renderHistory();
    fireEvent.click(await screen.findByRole("button", { name: "Import sessions" }));
    const dialog = within(await screen.findByRole("dialog", { name: "Import sessions" }));
    fireEvent.click(await dialog.findByRole("button", { name: "Import Old chat" }));
    expect(await dialog.findByText("Already imported")).toBeTruthy();
    fireEvent.click(dialog.getByRole("button", { name: "Close" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    mock.state.importCandidates = [];
    fireEvent.click(screen.getByRole("button", { name: "Import sessions" }));
    const empty = within(await screen.findByRole("dialog", { name: "Import sessions" }));
    expect(await empty.findByText(/No conversations from Claude Code or Codex/)).toBeTruthy();
  });
});
