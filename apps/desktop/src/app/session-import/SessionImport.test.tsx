// @vitest-environment jsdom
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { fakeRest } from "../../settings/fakeRest.ts";
import { SessionImport } from "./SessionImport.tsx";

describe("SessionImport", () => {
  it("previews a conversation and imports it, then opens the new session", async () => {
    const api = fakeRest();
    api.route("GET", "/api/session-import/candidates", {
      candidates: [
        {
          source: "codex",
          path: "/h/.codex/sessions/rollout-1.jsonl",
          title: "List files",
          cwd: "/w",
          modified: 1_700_000_000,
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
    const opened: string[] = [];
    render(<SessionImport api={api} profile="work" onImported={(id) => opened.push(id)} />);
    expect(screen.getByText("Reading from this computer")).toBeTruthy();
    expect(screen.getByText("Import into work")).toBeTruthy();
    expect(await screen.findByText("Pick a conversation")).toBeTruthy();
    fireEvent.click(await screen.findByText("List files"));
    expect(await screen.findByText("Codex · 2 messages")).toBeTruthy();
    expect(screen.getByText("List the files")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Continue in Indus" }));
    await waitFor(() => expect(opened).toEqual(["s-new"]));
    expect(api.calls.find((c) => c.path === "/api/session-import/import")?.body).toEqual({
      source: "codex",
      path: "/h/.codex/sessions/rollout-1.jsonl",
    });
  });

  it("filters by source and says when nothing is found", async () => {
    const api = fakeRest();
    api.route("GET", "/api/session-import/candidates", { candidates: [] });
    api.route("GET", "/api/session-import/candidates?source=claude-code", { candidates: [] });
    render(<SessionImport api={api} profile={null} host="box.example:8080" />);
    expect(screen.getByText("Reading from box.example:8080")).toBeTruthy();
    expect(await screen.findByText("No conversations found")).toBeTruthy();
    fireEvent.click(screen.getByRole("radio", { name: "Claude Code" }));
    await waitFor(() =>
      expect(
        api.calls.some((c) => c.path === "/api/session-import/candidates?source=claude-code"),
      ).toBe(true),
    );
  });
});
