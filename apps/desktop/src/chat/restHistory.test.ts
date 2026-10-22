import { describe, expect, it } from "vitest";
import type { IndusApiRequest, IndusApiResult } from "../global.d.ts";
import { displayRows, HistoryRouteMissing, restHistory } from "./restHistory.ts";
import { type TranscriptSource, TranscriptStore } from "./transcriptStore.ts";

const ROWS = [
  { id: 1, role: "system", content: "rules" },
  { id: 2, role: "user", content: "Please write the notes" },
  {
    id: 3,
    role: "assistant",
    content: "",
    tool_calls: [{ id: "c1", name: "write_file", arguments: "{}" }],
  },
  { id: 4, role: "tool", content: "wrote 1 file", tool_call_id: "c1" },
  { id: 5, role: "assistant", content: "Done." },
];

describe("REST history", () => {
  it("shows user, assistant and tool rows; drops system and tool-call-only rows; names tools", () => {
    expect(displayRows(ROWS)).toEqual([
      { role: "user", text: "Please write the notes", row_id: 2 },
      { role: "tool", text: "wrote 1 file", name: "write_file", arguments: {}, row_id: 4 },
      { role: "assistant", text: "Done.", row_id: 5 },
    ]);
  });

  it("gives tool rows their call's arguments, as the live rows have them (W47-a)", () => {
    const rows = [
      {
        id: 1,
        role: "assistant",
        content: "",
        tool_calls: [
          { id: "c1", name: "read_file", arguments: '{"path": "notes.md"}' },
          { id: "c2", function: { name: "terminal", arguments: '{"command": "ls"}' } },
          { id: "c3", name: "odd", arguments: "not json" },
          { id: "c4", name: "list", arguments: "[1, 2]" },
        ],
      },
      { id: 2, role: "tool", content: "text", tool_call_id: "c1" },
      { id: 3, role: "tool", content: "ran", tool_call_id: "c2" },
      { id: 4, role: "tool", content: "?", tool_call_id: "c3" },
      { id: 5, role: "tool", content: "?", tool_call_id: "c4" },
    ];
    expect(displayRows(rows)).toEqual([
      { role: "tool", text: "text", name: "read_file", arguments: { path: "notes.md" }, row_id: 2 },
      { role: "tool", text: "ran", name: "terminal", arguments: { command: "ls" }, row_id: 3 },
      { role: "tool", text: "?", name: "odd", row_id: 4 },
      { role: "tool", text: "?", name: "list", row_id: 5 },
    ]);
  });

  it("asks the session's messages route for the profile; not-found means the route is missing", async () => {
    const seen: IndusApiRequest[] = [];
    const load = restHistory(async (request) => {
      seen.push(request);
      return { kind: "response", status: 200, body: { messages: ROWS } } as IndusApiResult;
    }, "work");
    expect((await load("s/1")).length).toBe(3);
    expect(seen[0]).toEqual({
      method: "GET",
      path: "/api/sessions/s%2F1/messages",
      profile: "work",
    });
    const missing = restHistory(
      async () => ({ kind: "not-found", status: 404, body: {} }) as IndusApiResult,
      null,
    );
    await expect(missing("s1")).rejects.toBeInstanceOf(HistoryRouteMissing);
    const broken = restHistory(
      async () => ({ kind: "response", status: 500, body: {} }) as IndusApiResult,
      null,
    );
    await expect(broken("s1")).rejects.toThrow(/500/);
  });

  it("the store falls back to session.history only when the route is missing", async () => {
    let wsCalls = 0;
    const source: TranscriptSource = {
      onEvent: () => () => undefined,
      request: (async () => {
        wsCalls += 1;
        return { messages: [{ role: "user", text: "from ws" }] };
      }) as unknown as TranscriptSource["request"],
      watchSession: async () => undefined,
      unwatchSession: () => undefined,
    };
    const viaRest = new TranscriptStore(source, {
      history: async () => [{ role: "user", text: "from rest" }],
    });
    await viaRest.hydrate("s1");
    expect(wsCalls).toBe(0);
    const viaWs = new TranscriptStore(source, {
      history: async () => {
        throw new HistoryRouteMissing("none");
      },
    });
    await viaWs.hydrate("s1");
    expect(wsCalls).toBe(1);
    viaRest.dispose();
    viaWs.dispose();
  });
});
