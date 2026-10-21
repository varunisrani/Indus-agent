// @vitest-environment jsdom
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";
import { fakeRest } from "../../settings/fakeRest.ts";
import { type ArtifactsBridge, ArtifactsPage } from "./ArtifactsPage.tsx";
import { extractArtifacts } from "./artifacts.ts";

const MESSAGES = [
  { role: "user", content: "make a chart and a report", timestamp: 10 },
  {
    role: "assistant",
    content: "",
    timestamp: 11,
    tool_calls: [
      {
        id: "1",
        name: "write_file",
        arguments: JSON.stringify({ path: "/srv/work/report.md", content: "x" }),
      },
      { id: "2", name: "patch", arguments: "not json" },
    ],
  },
  { role: "tool", content: "saved chart to /srv/work/out/chart.png", timestamp: 12 },
  {
    role: "assistant",
    content:
      "Here: ![chart](https://cdn.example.com/c.png) and the docs https://docs.example.com/guide.",
    timestamp: 13,
  },
];

describe("artifacts", () => {
  it("finds images, files and links and jumps back to the session", async () => {
    const found = extractArtifacts(MESSAGES, { id: "s1", title: "Charts", profile: "work" });
    expect(found.map((a) => [a.kind, a.value])).toEqual([
      ["file", "/srv/work/report.md"],
      ["image", "/srv/work/out/chart.png"],
      ["image", "https://cdn.example.com/c.png"],
      ["link", "https://docs.example.com/guide"],
    ]);
    const api = fakeRest();
    api.route("GET", "/api/sessions?limit=30", {
      sessions: [
        { id: "s1", title: "Charts" },
        { id: "s2", title: null },
      ],
    });
    api.route("GET", "/api/sessions/s1/messages?limit=200&order=latest", { messages: MESSAGES });
    api.route("GET", "/api/sessions/s2/messages?limit=200&order=latest", () => ({
      status: 500,
      body: {},
    }));
    const opened: string[] = [];
    render(
      <MemoryRouter>
        <ArtifactsPage
          api={api}
          profile="work"
          bridge={null}
          onOpenSession={(id) => opened.push(id)}
        />
      </MemoryRouter>,
    );
    expect(await screen.findByText("report.md")).toBeTruthy();
    expect(screen.getByText("1 sessions could not be read.")).toBeTruthy();
    fireEvent.click(screen.getByText("Files"));
    expect(screen.queryByText("guide")).toBeNull();
    fireEvent.click(screen.getAllByText("Open chat")[0] as HTMLElement);
    expect(opened).toEqual(["s1"]);
  });

  it("a remote artifact download carries the originating profile", async () => {
    const api = fakeRest();
    api.route("GET", "/api/sessions?limit=30", { sessions: [{ id: "s9", title: "Remote run" }] });
    api.route("GET", "/api/sessions/s9/messages?limit=200&order=latest", { messages: MESSAGES });
    const downloads: unknown[] = [];
    const bridge: ArtifactsBridge = {
      artifacts: {
        download: async (request) => {
          downloads.push(request);
          return { savedTo: "/home/me/Downloads/report.md" };
        },
      },
      external: { open: async () => ({ ok: true }) },
    };
    render(
      <MemoryRouter>
        <ArtifactsPage api={api} profile="research" bridge={bridge} />
      </MemoryRouter>,
    );
    const card = (await screen.findByText("report.md")).closest("[data-kind]") as HTMLElement;
    fireEvent.click(
      Array.from(card.querySelectorAll("button")).find(
        (b) => b.textContent === "Download",
      ) as HTMLElement,
    );
    await waitFor(() => expect(downloads).toHaveLength(1));
    expect(downloads[0]).toEqual({
      path: "/srv/work/report.md",
      profile: "research",
      sessionId: "s9",
    });
    expect(await screen.findByText("Saved to /home/me/Downloads/report.md")).toBeTruthy();
  });
});
