// @vitest-environment jsdom
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { fakeRest } from "../../settings/fakeRest.ts";
import { EMPTY_DELEGATION, reduceDelegation, totals } from "../agents/delegation.ts";
import { CommandCenter, filterByLevel } from "./CommandCenter.tsx";

describe("delegation model", () => {
  it("the delegation reducer counts workers, tools and files", () => {
    let state = EMPTY_DELEGATION;
    const step = (event: Record<string, unknown> & { type: string }) => {
      state = reduceDelegation(state, event, 1000);
    };
    step({
      type: "tool.started",
      call_id: "c1",
      name: "delegate_task",
      arguments: { tasks: [{ goal: "fix the parser" }, { goal: "write tests" }] },
    });
    step({ type: "tool.started", call_id: "c2", name: "terminal", arguments: { command: "ls" } });
    step({ type: "tool.progress", call_id: "c1", message: "[1] read_file: src/parser.py" });
    step({ type: "tool.progress", call_id: "c1", message: "[1] patch: src/parser.py" });
    step({ type: "tool.progress", call_id: "c1", message: "[2] write_file: tests/test_parser.py" });
    step({ type: "tool.progress", call_id: "c1", message: "[2] thinking about edge cases" });
    expect(totals(state)).toEqual({ workers: 2, active: 2, tools: 3, files: 2 });
    step({ type: "subagent.update", parent_call_id: "c1", index: 2, status: "failed" });
    step({
      type: "tool.completed",
      call_id: "c1",
      ok: true,
      name: "delegate_task",
      duration_ms: 5,
      result: "",
    });
    const workers = state.batches[0]?.workers ?? [];
    expect(workers.map((w) => [w.goal, w.status, w.tools, w.files])).toEqual([
      ["fix the parser", "done", 2, ["src/parser.py"]],
      ["write tests", "failed", 1, ["tests/test_parser.py"]],
    ]);
    expect(totals(state).active).toBe(0);
    // a list/steer call opens no batch
    const same = reduceDelegation(
      state,
      { type: "tool.started", call_id: "c3", name: "delegate_task", arguments: { action: "list" } },
      1,
    );
    expect(same).toBe(state);
  });
});

describe("CommandCenter", () => {
  it("the log viewer filters by level", async () => {
    const api = fakeRest();
    api.route("GET", "/api/daemon", { running: true, state: "running" });
    api.route("GET", "/api/status", { version: "1.0.0", active_sessions: 2 });
    api.route("GET", "/api/logs?file=agent&lines=200", {
      file: "agent",
      lines: ["a INFO one", "b ERROR two"],
    });
    api.route("GET", "/api/logs?file=agent&lines=200&level=ERROR", {
      file: "agent",
      lines: ["b ERROR two"],
    });
    const bridge = {
      logs: {
        recent: async () => ({
          lines: [
            "t1 DEBUG noisy",
            "t2 WARNING disk low",
            "  at frame 1",
            "t3 INFO ok",
            "t4 ERROR broke",
          ],
        }),
        reveal: async () => ({ ok: true as const }),
        rendererError: () => undefined,
      },
    };
    render(<CommandCenter api={api} profile="work" bridge={bridge} initial="system" />);
    expect(await screen.findByText("Gateway running")).toBeTruthy();
    expect(await screen.findByText("Indus 1.0.0 · Active sessions 2")).toBeTruthy();
    await waitFor(() => expect(screen.getByTestId("log-lines").textContent).toContain("one"));
    fireEvent.click(screen.getByRole("tab", { name: "error" }));
    await waitFor(() => expect(screen.getByTestId("log-lines").textContent).toBe("b ERROR two"));
    expect(api.calls.at(-1)).toMatchObject({
      profile: "work",
      path: "/api/logs?file=agent&lines=200&level=ERROR",
    });
    fireEvent.click(screen.getByRole("tab", { name: "desktop" }));
    await waitFor(() => expect(screen.getByTestId("log-lines").textContent).toBe("t4 ERROR broke"));
    fireEvent.click(screen.getByRole("tab", { name: "warning" }));
    await waitFor(() =>
      expect(screen.getByTestId("log-lines").textContent).toBe(
        "t2 WARNING disk low\n  at frame 1\nt4 ERROR broke",
      ),
    );
    // a warning line carries the amber rule, an error line the red one
    const marked = Array.from(document.querySelectorAll(".kit-log-line[data-level]")).map((el) =>
      el.getAttribute("data-level"),
    );
    expect(marked).toEqual(["warn", "error"]);
    expect(filterByLevel(["x"], "ALL")).toEqual(["x"]);
  });

  it("lists sessions newest first, opens one, and deletes after a confirm", async () => {
    const api = fakeRest();
    api.route("GET", "/api/sessions?order=recent&limit=50&offset=0", {
      sessions: [
        { id: "s1", title: "Fix the parser", last_active: 1_700_000_000, pinned: false },
        { id: "s2", title: "Write release notes", last_active: 1_699_000_000 },
      ],
      total: 2,
    });
    api.route("DELETE", "/api/sessions/s2", { ok: true });
    api.route("PATCH", "/api/sessions/s1", { ok: true });
    const opened: string[] = [];
    render(
      <CommandCenter
        api={api}
        profile="work"
        bridge={null}
        onOpenSession={(id) => opened.push(id)}
      />,
    );
    fireEvent.click(await screen.findByText("Fix the parser"));
    expect(opened).toEqual(["s1"]);
    fireEvent.click(screen.getByLabelText("Pin Fix the parser"));
    await waitFor(() => expect(screen.getByLabelText("Unpin Fix the parser")).toBeTruthy());
    expect(api.calls.find((c) => c.method === "PATCH")?.body).toEqual({ pinned: true });
    fireEvent.click(screen.getByLabelText("Delete Write release notes"));
    fireEvent.click(await screen.findByRole("button", { name: "Delete" }));
    await waitFor(() => expect(screen.queryByText("Write release notes")).toBeNull());
    expect(api.calls.some((c) => c.method === "DELETE" && c.path === "/api/sessions/s2")).toBe(
      true,
    );
    // the search filters on title and id
    fireEvent.change(screen.getByLabelText("Search sessions"), { target: { value: "zzz" } });
    expect(await screen.findByText("No sessions match this search.")).toBeTruthy();
  });

  it("usage shows the tiles, a dash for API calls and the cost as a hint", async () => {
    const api = fakeRest();
    api.route("GET", "/api/analytics/usage?days=30", {
      daily: [
        { day: "2026-10-23", input_tokens: 1200, output_tokens: 300, cost: 0.1, sessions: 1 },
        { day: "2026-10-24", input_tokens: 800, output_tokens: 100, cost: 0.05, sessions: 1 },
      ],
      totals: { input_tokens: 2000, output_tokens: 400, cost: 0.15, sessions: 2 },
    });
    api.route("GET", "/api/analytics/usage?days=7", {
      daily: [],
      totals: { input_tokens: 0, output_tokens: 0, cost: 0, sessions: 0 },
    });
    render(<CommandCenter api={api} profile="work" bridge={null} initial="usage" />);
    expect(await screen.findByText("2K / 400")).toBeTruthy();
    expect(screen.getByText("—")).toBeTruthy();
    expect(screen.getByText("0.15 USD estimated")).toBeTruthy();
    expect(screen.getByText("2026-10-23")).toBeTruthy();
    fireEvent.click(screen.getByRole("radio", { name: "7d" }));
    expect(await screen.findByText("Nothing was used in this period.")).toBeTruthy();
  });

  it("usage shows the counted API calls and the top models (W58-J1-1)", async () => {
    const api = fakeRest();
    const model = (name: string, tokens: number, calls: number) => ({
      model: name,
      input_tokens: tokens,
      output_tokens: 0,
      api_calls: calls,
      cost: 0,
    });
    api.route("GET", "/api/analytics/usage?days=30", {
      daily: [{ day: "2026-10-24", input_tokens: 4900, output_tokens: 576, cost: 0, sessions: 5 }],
      totals: { input_tokens: 4900, output_tokens: 576, cost: 0, sessions: 5, api_calls: 6 },
      by_model: [
        model("big-model", 5400, 4),
        model("m2", 50, 1),
        model("m3", 40, 1),
        model("m4", 30, 1),
        model("m5", 20, 1),
        model("m6", 10, 1),
      ],
    });
    api.route("GET", "/api/analytics/usage?days=7", {
      daily: [],
      totals: { input_tokens: 0, output_tokens: 0, cost: 0, sessions: 0, api_calls: 0 },
      by_model: [],
    });
    render(<CommandCenter api={api} profile="work" bridge={null} initial="usage" />);
    expect(await screen.findByText("4.9K / 576")).toBeTruthy();
    expect(screen.getByText("6")).toBeTruthy();
    expect(screen.queryByText("not counted yet")).toBeNull();
    const top = screen.getByRole("list", { name: "Top models" });
    expect(within(top).getAllByRole("listitem")).toHaveLength(5);
    expect(within(top).getByText("big-model")).toBeTruthy();
    expect(within(top).getByText("5.4K")).toBeTruthy();
    expect(within(top).queryByText("m6")).toBeNull();
    fireEvent.click(screen.getByRole("radio", { name: "7d" }));
    expect(await screen.findByText("No model was used in this period.")).toBeTruthy();
  });

  it("maintenance answers 409 with who holds the gate", async () => {
    const api = fakeRest();
    api.route("POST", "/api/sessions/prune", () => ({
      status: 409,
      body: {
        detail: { code: "daemon_running", message: "The daemon owns this profile's sessions" },
      },
    }));
    api.route("GET", "/api/memory", {
      active: "",
      builtin_files: { "MEMORY.md": 2048, "USER.md": 0 },
    });
    let imports = 0;
    render(
      <CommandCenter
        api={api}
        profile="work"
        bridge={null}
        initial="maintenance"
        onOpenImport={() => {
          imports += 1;
        }}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Prune old sessions" }));
    expect(
      await screen.findByText(/Not now: The daemon owns this profile's sessions/),
    ).toBeTruthy();
    expect(await screen.findByText("2.0 KB")).toBeTruthy();
    expect(screen.getByText("empty")).toBeTruthy();
    expect(
      (screen.getByRole("button", { name: "Reset memory" }) as HTMLButtonElement).disabled,
    ).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Open" }));
    expect(imports).toBe(1);
  });
});
