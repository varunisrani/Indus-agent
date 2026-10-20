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

describe("delete", () => {
  for (const [code, message] of Object.entries(CONFLICT_MESSAGES)) {
    it(`explains a 409 ${code} in plain language`, async () => {
      mock.state.deleteConflicts["s-chat-1"] = code;
      renderPage(<SessionsPage />, "/sessions");
      fireEvent.click((await row("s-chat-1")).getByRole("button", { name: "Delete" }));
      const dialog = await screen.findByRole("dialog");
      fireEvent.click(within(dialog).getByRole("button", { name: "Delete" }));
      expect((await within(dialog).findByRole("alert")).textContent).toBe(message);
      expect(screen.getByTestId("session-s-chat-1")).toBeTruthy();
    });
  }

  it("sends the snapshot identity and removes the row", async () => {
    renderPage(<SessionsPage />, "/sessions");
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
    renderPage(<SessionsPage />, "/sessions");
    fireEvent.click(await screen.findByRole("button", { name: "Prune…" }));
    const dialog = await screen.findByRole("dialog");
    const pruneButton = within(dialog).getByRole("button", { name: /^Prune/ });
    expect((pruneButton as HTMLButtonElement).disabled).toBe(true);

    fireEvent.change(within(dialog).getByLabelText("Older than (days)"), {
      target: { value: "30" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Check" }));
    expect((await within(dialog).findByTestId("prune-preview")).textContent).toContain(
      "3 sessions would be removed",
    );
    const dry = mock.callsTo("POST", "/api/sessions/prune");
    expect(dry).toHaveLength(1);
    expect(dry[0]?.body).toEqual({ older_than_days: 30, dry_run: true });

    fireEvent.click(within(dialog).getByRole("button", { name: "Prune 3" }));
    expect(await screen.findByText("Removed 3 sessions.")).toBeTruthy();
    expect(mock.callsTo("POST", "/api/sessions/prune")[1]?.body).toEqual({
      older_than_days: 30,
      dry_run: false,
    });
  });

  it("refuses a day count that is not a whole number", async () => {
    renderPage(<SessionsPage />, "/sessions");
    fireEvent.click(await screen.findByRole("button", { name: "Prune…" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText("Older than (days)"), {
      target: { value: "-4" },
    });
    expect(within(dialog).getByText("Enter a whole number of days.")).toBeTruthy();
    expect(
      (within(dialog).getByRole("button", { name: "Check" }) as HTMLButtonElement).disabled,
    ).toBe(true);
  });
});

describe("browse", () => {
  it("resumes a session in Chat with its id", async () => {
    renderPage(<SessionsPage />, "/sessions?profile=work");
    fireEvent.click((await row("s-chat-1")).getByRole("button", { name: /Resume/ }));
    await waitFor(() => expect(currentLocation()).toBe("/chat?resume=s-chat-1&profile=work"));
  });

  it("filters by tab: chats exclude automation, automation includes only it", async () => {
    renderPage(<SessionsPage />, "/sessions");
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

  it("filters by an exact source", async () => {
    renderPage(<SessionsPage />, "/sessions");
    await row("s-chat-1");
    const select = screen.getByLabelText("Source");
    await waitFor(() => expect(within(select).getByRole("option", { name: "web" })).toBeTruthy());
    fireEvent.change(select, { target: { value: "web" } });
    await waitFor(() => expect(screen.queryByTestId("session-s-chat-1")).toBeNull());
    expect(mock.callsTo("GET", "/api/sessions").at(-1)?.query.get("source")).toBe("web");
  });

  it("renders search snippets with marks and scrolls the opened hit to its first match", async () => {
    const scrolled = vi.fn();
    Element.prototype.scrollIntoView = scrolled;
    renderPage(<SessionsPage />, "/sessions");
    fireEvent.change(await screen.findByLabelText("Search sessions"), {
      target: { value: "parser" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    const results = await screen.findByRole("list", { name: "Search results" });
    const mark = await within(results).findByText("parser", { selector: "mark" });
    expect(mark.tagName).toBe("MARK");
    // No HTML is injected: the snippet's surrounding text is plain text nodes.
    expect(results.innerHTML).not.toContain("&lt;mark&gt;");

    fireEvent.click(within(results).getByRole("button"));
    const detail = await screen.findByTestId("detail-s-chat-1");
    expect(
      (await within(detail).findAllByText("parser", { selector: "mark" })).length,
    ).toBeGreaterThan(0);
    await waitFor(() => expect(scrolled).toHaveBeenCalled());
  });

  it("shows the history with tool calls folded", async () => {
    renderPage(<SessionsPage />, "/sessions");
    fireEvent.click((await row("s-chat-1")).getByRole("button", { name: "Refactor the parser" }));
    const detail = await screen.findByTestId("detail-s-chat-1");
    expect(await within(detail).findByText("Tool call: read_file")).toBeTruthy();
    expect(within(detail).getByText("Result from read_file")).toBeTruthy();
    expect(within(detail).getByRole("heading", { name: "Done" })).toBeTruthy();
  });

  it("renames with the mutation identity", async () => {
    renderPage(<SessionsPage />, "/sessions");
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
    renderPage(<SessionsPage />, "/sessions");
    const stats = await screen.findByLabelText("Session statistics");
    await waitFor(() =>
      expect(within(stats).getByText("Sessions").nextSibling?.textContent).toBe("3"),
    );
  });
});
