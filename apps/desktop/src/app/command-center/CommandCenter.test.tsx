// @vitest-environment jsdom
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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
    api.route("GET", "/api/sessions/stats", { total: 3, active: 1, empty: 1 });
    api.route("GET", "/api/sessions?limit=10", { sessions: [] });
    api.route("GET", "/api/logs?file=agent&lines=500", {
      file: "agent",
      lines: ["a INFO one", "b ERROR two"],
    });
    api.route("GET", "/api/logs?file=agent&lines=500&level=ERROR", {
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
    render(<CommandCenter api={api} profile="work" bridge={bridge} />);
    fireEvent.click(screen.getByRole("tab", { name: "Logs" }));
    await waitFor(() => expect(screen.getByTestId("log-lines").textContent).toContain("one"));
    fireEvent.change(screen.getByLabelText("Level"), { target: { value: "ERROR" } });
    await waitFor(() => expect(screen.getByTestId("log-lines").textContent).toBe("b ERROR two"));
    expect(api.calls.at(-1)).toMatchObject({
      profile: "work",
      path: "/api/logs?file=agent&lines=500&level=ERROR",
    });
    fireEvent.change(screen.getByLabelText("Log"), { target: { value: "desktop" } });
    await waitFor(() => expect(screen.getByTestId("log-lines").textContent).toBe("t4 ERROR broke"));
    fireEvent.change(screen.getByLabelText("Level"), { target: { value: "WARNING" } });
    await waitFor(() =>
      expect(screen.getByTestId("log-lines").textContent).toBe(
        "t2 WARNING disk low\n  at frame 1\nt4 ERROR broke",
      ),
    );
    expect(filterByLevel(["x"], "ALL")).toEqual(["x"]);
  });

  it("maintenance answers 409 with who holds the gate, and the session import previews and imports", async () => {
    const api = fakeRest();
    api.route("POST", "/api/sessions/prune", {});
    api.route("POST", "/api/sessions/prune", () => ({
      status: 409,
      body: {
        detail: { code: "daemon_running", message: "The daemon owns this profile's sessions" },
      },
    }));
    api.route("GET", "/api/session-import/candidates", {
      candidates: [
        {
          source: "codex",
          path: "/h/.codex/sessions/rollout-1.jsonl",
          title: "List files",
          cwd: "/w",
          modified: 1,
        },
      ],
    });
    api.route("POST", "/api/session-import/preview", {
      title: "List files",
      messages: [
        { role: "user", content: "List the files" },
        { role: "assistant", content: "", tool: "shell" },
      ],
      skipped: 0,
    });
    api.route("POST", "/api/session-import/import", {
      session_id: "s-new",
      title: "List files",
      message_count: 2,
    });
    render(<CommandCenter api={api} profile="work" bridge={null} initial="maintenance" />);
    fireEvent.click(screen.getByText("Prune old sessions"));
    expect(
      await screen.findByText(/Not now: The daemon owns this profile's sessions/),
    ).toBeTruthy();
    fireEvent.click(await screen.findByText("List files"));
    expect(await screen.findByText("List files: 2 messages")).toBeTruthy();
    fireEvent.click(screen.getByText("Import this session"));
    expect(await screen.findByText("Imported 2 messages.")).toBeTruthy();
    expect(api.calls.find((c) => c.path === "/api/session-import/import")?.body).toEqual({
      source: "codex",
      path: "/h/.codex/sessions/rollout-1.jsonl",
    });
  });
});
