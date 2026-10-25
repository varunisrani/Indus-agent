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
    const row = (await screen.findByText("paper digest")).closest("li") as HTMLElement;
    expect(row.textContent).toContain("research"); // the owner as the row's meta
    fireEvent.click(row.querySelector("button") as HTMLButtonElement); // select it
    fireEvent.click(await screen.findByRole("button", { name: "Pause" }));
    await waitFor(() =>
      expect(api.calls.some((c) => c.path === "/api/cron/jobs/r1/pause")).toBe(true),
    );
    const pause = api.calls.find((c) => c.path === "/api/cron/jobs/r1/pause");
    expect(pause?.profile).toBe("research"); // never the window's own profile
  });

  it("a new job can start from a blueprint", async () => {
    const api = backend();
    render(<CronOverlay api={api} profile="work" onClose={() => {}} />);
    await screen.findByText("standup notes");
    fireEvent.click(screen.getAllByRole("button", { name: "New cron" })[0] as HTMLElement);
    fireEvent.change(await screen.findByLabelText("Start from"), {
      target: { value: "weekly-review" },
    });
    // the blueprint's expression is a preset: the summary names it
    expect(screen.getByText("Every Monday at 9:00 AM")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Create cron" }));
    await waitFor(() =>
      expect(api.calls.some((c) => c.method === "POST" && c.path === "/api/cron/jobs")).toBe(true),
    );
    const created = api.calls.find((c) => c.method === "POST" && c.path === "/api/cron/jobs");
    expect(created).toMatchObject({
      profile: "work",
      body: { prompt: "Review the week", schedule: "0 9 * * 1", skills: ["weekly-review"] },
    });
    expect(((created?.body ?? {}) as Record<string, unknown>).model).toBeUndefined();
  });

  it("a blueprint row opens the dialog pre-set, and delivery targets become checkboxes", async () => {
    const api = backend();
    api.route("GET", "/api/cron/delivery-targets", {
      targets: [
        { id: "local", label: "Save locally", kind: "local", available: true },
        { id: "telegram", label: "Telegram", kind: "platform", available: true },
        { id: "slack", label: "Slack", kind: "platform", available: false },
      ],
    });
    render(<CronOverlay api={api} profile="work" onClose={() => {}} />);
    fireEvent.click(await screen.findByText("weekly-review"));
    expect(((await screen.findByLabelText("Start from")) as HTMLSelectElement).value).toBe(
      "weekly-review",
    );
    await waitFor(() => expect(screen.getByLabelText("This computer")).toBeTruthy());
    expect((screen.getByLabelText(/Slack/) as HTMLInputElement).disabled).toBe(true);
    fireEvent.click(screen.getByLabelText("This computer"));
    fireEvent.click(screen.getByLabelText("Telegram"));
    fireEvent.change(screen.getByLabelText("Frequency"), { target: { value: "custom" } });
    fireEvent.change(screen.getByLabelText("Cron expression"), { target: { value: "every 2h" } });
    fireEvent.click(screen.getByRole("button", { name: "Create cron" }));
    await waitFor(() =>
      expect(api.calls.some((c) => c.method === "POST" && c.path === "/api/cron/jobs")).toBe(true),
    );
    const created = api.calls.find((c) => c.method === "POST" && c.path === "/api/cron/jobs");
    expect(created?.body).toMatchObject({ schedule: "every 2h", deliver: "local,telegram" });
  });
});
