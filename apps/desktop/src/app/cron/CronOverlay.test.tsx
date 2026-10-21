// @vitest-environment jsdom
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { fakeRest } from "../../settings/fakeRest.ts";
import { CronOverlay } from "./CronOverlay.tsx";

function job(id: string, name: string, state = "scheduled") {
  return {
    id,
    name,
    prompt: "summarise the news",
    schedule: "every 2h",
    schedule_display: "every 2h",
    deliver: "local",
    skills: [],
    state,
    enabled: true,
    paused_reason: null,
    last_run_at: null,
    next_run_at: null,
    last_status: "ok",
    last_error: null,
  };
}

function backend() {
  const api = fakeRest();
  api.route("GET", "/api/profiles", { profiles: [{ name: "work" }, { name: "research" }] });
  api.route("GET", "/api/cron/jobs", (request) => ({
    status: 200,
    body: {
      jobs:
        request.profile === "research" ? [job("r1", "paper digest")] : [job("w1", "standup notes")],
    },
  }));
  api.route("GET", "/api/cron/blueprints", {
    blueprints: [
      {
        skill: "weekly-review",
        description: "Review the week",
        schedule: "0 9 * * 1",
        prompt: null,
      },
    ],
  });
  api.route("POST", "/api/cron/jobs/r1/pause", { ok: true });
  api.route("POST", "/api/cron/jobs", { ok: true });
  return api;
}

describe("CronOverlay", () => {
  it("the aggregated cron list sends pause to the owning profile", async () => {
    const api = backend();
    render(<CronOverlay api={api} profile="work" onClose={() => {}} />);
    expect(await screen.findByText("standup notes")).toBeTruthy();
    expect(screen.queryByText("paper digest")).toBeNull();
    fireEvent.click(screen.getByLabelText("All profiles"));
    const row = (await screen.findByText("paper digest")).closest("tr") as HTMLElement;
    expect(row.textContent).toContain("research");
    fireEvent.click(row.querySelector("button") as HTMLButtonElement); // Pause
    await waitFor(() =>
      expect(api.calls.some((c) => c.path === "/api/cron/jobs/r1/pause")).toBe(true),
    );
    const pause = api.calls.find((c) => c.path === "/api/cron/jobs/r1/pause");
    expect(pause?.profile).toBe("research"); // never the window's own profile
  });

  it("a new job can start from a blueprint", async () => {
    const api = backend();
    render(<CronOverlay api={api} profile="work" onClose={() => {}} />);
    fireEvent.click(await screen.findByText("New job"));
    fireEvent.change(await screen.findByLabelText("Start from"), {
      target: { value: "weekly-review" },
    });
    fireEvent.click(screen.getByText("Save"));
    await waitFor(() =>
      expect(api.calls.some((c) => c.method === "POST" && c.path === "/api/cron/jobs")).toBe(true),
    );
    const created = api.calls.find((c) => c.method === "POST" && c.path === "/api/cron/jobs");
    expect(created).toMatchObject({
      profile: "work",
      body: { prompt: "Review the week", schedule: "0 9 * * 1", skills: ["weekly-review"] },
    });
  });
});
