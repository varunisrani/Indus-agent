import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createMockApi, isValidSchedule, type MockApi } from "../testing/mockApi.ts";
import { renderPage } from "../testing/render.tsx";
import { CronPage } from "./CronPage.tsx";
import { builderProblem, buildSchedule, DEFAULT_CHOICE } from "./cron/scheduleBuilder.ts";

let mock: MockApi;
let restore: () => void;

beforeEach(() => {
  mock = createMockApi();
  mock.state.targets = [
    { id: "local", label: "Save locally", kind: "local", available: true },
    { id: "telegram", label: "Telegram home chat", kind: "platform", available: true },
    { id: "email", label: "Email", kind: "email", available: false },
  ];
  restore = mock.install();
  vi.spyOn(console, "warn").mockImplementation(() => {});
});

afterEach(() => {
  restore();
});

async function openNewJob() {
  renderPage(<CronPage />, "/cron");
  fireEvent.click(await screen.findByRole("button", { name: "New job" }));
  return within(await screen.findByRole("dialog"));
}

describe("schedule builder", () => {
  it("emits our grammar strings, which the store accepts", () => {
    const every = buildSchedule({ ...DEFAULT_CHOICE, kind: "every", amount: 30, unit: "m" });
    const once = buildSchedule({ ...DEFAULT_CHOICE, kind: "in", amount: 2, unit: "h" });
    const daily = buildSchedule({ ...DEFAULT_CHOICE, kind: "daily", time: "09:30" });
    const weekdays = buildSchedule({ ...DEFAULT_CHOICE, kind: "weekdays", time: "18:00" });
    const cron = buildSchedule({ ...DEFAULT_CHOICE, kind: "cron", cron: " 0  9 * * 1-5 " });
    expect([every, once, daily, weekdays, cron]).toEqual([
      "every 30m",
      "in 2h",
      "daily at 09:30",
      "weekdays at 18:00",
      "0 9 * * 1-5",
    ]);
    for (const schedule of [every, once, daily, weekdays, cron])
      expect(isValidSchedule(schedule)).toBe(true);
  });

  it("refuses choices that cannot become a schedule", () => {
    expect(builderProblem({ ...DEFAULT_CHOICE, kind: "every", amount: 0 })).not.toBeNull();
    expect(builderProblem({ ...DEFAULT_CHOICE, kind: "in", amount: 1.5 })).not.toBeNull();
    expect(builderProblem({ ...DEFAULT_CHOICE, kind: "daily", time: "25:00" })).not.toBeNull();
    expect(builderProblem({ ...DEFAULT_CHOICE, kind: "cron", cron: "0 9 * *" })).not.toBeNull();
    expect(isValidSchedule("whenever")).toBe(false);
  });
});

describe("cron page", () => {
  it("creates a job with the builder's every 30m", async () => {
    const dialog = await openNewJob();
    fireEvent.change(dialog.getByLabelText("Prompt"), { target: { value: "Summarise my inbox" } });
    fireEvent.click(dialog.getByRole("button", { name: "Use" }));
    expect((dialog.getByLabelText("Schedule") as HTMLInputElement).value).toBe("every 30m");
    fireEvent.change(dialog.getByLabelText("Deliver to"), { target: { value: "telegram" } });
    fireEvent.click(dialog.getByRole("button", { name: "Create" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(mock.callsTo("POST", "/api/cron/jobs")[0]?.body).toEqual({
      name: "",
      prompt: "Summarise my inbox",
      schedule: "every 30m",
      deliver: "telegram",
    });
    expect(await screen.findByText("every 30m")).toBeTruthy();
  });

  it("creates a one-shot job with the builder's in 2h", async () => {
    const dialog = await openNewJob();
    fireEvent.change(dialog.getByLabelText("Prompt"), { target: { value: "Remind me" } });
    fireEvent.change(dialog.getByLabelText("Kind"), { target: { value: "in" } });
    fireEvent.change(dialog.getByLabelText("Amount"), { target: { value: "2" } });
    fireEvent.change(dialog.getByLabelText("Unit"), { target: { value: "h" } });
    expect(dialog.getByText("in 2h")).toBeTruthy();
    fireEvent.click(dialog.getByRole("button", { name: "Use" }));
    fireEvent.click(dialog.getByRole("button", { name: "Create" }));
    await waitFor(() => expect(mock.state.cronJobs).toHaveLength(1));
    expect(mock.state.cronJobs[0]?.schedule).toBe("in 2h");
  });

  it("keeps the modal open with the server's reason for a bad schedule", async () => {
    const dialog = await openNewJob();
    fireEvent.change(dialog.getByLabelText("Prompt"), { target: { value: "Do it" } });
    fireEvent.change(dialog.getByLabelText("Schedule"), { target: { value: "whenever" } });
    fireEvent.click(dialog.getByRole("button", { name: "Create" }));
    expect((await dialog.findByRole("alert")).textContent).toContain(
      "'whenever' is not a schedule",
    );
    expect(screen.getByRole("dialog")).toBeTruthy();
  });

  it("marks delivery targets that are not set up", async () => {
    const dialog = await openNewJob();
    const email = within(dialog.getByLabelText("Deliver to")).getByRole("option", {
      name: /Email/,
    }) as HTMLOptionElement;
    expect(email.disabled).toBe(true);
  });

  it("pauses, resumes, runs now and deletes after confirming", async () => {
    mock.state.cronJobs = [
      {
        id: "job-1",
        name: "Digest",
        prompt: "Digest",
        schedule: "every 1d",
        schedule_display: "every 1d",
        deliver: "local",
        state: "scheduled",
        enabled: true,
        last_run_at: null,
        next_run_at: null,
        last_status: null,
      },
    ];
    renderPage(<CronPage />, "/cron");
    const job = within(await screen.findByTestId("job-job-1"));
    expect(job.getByText("scheduled")).toBeTruthy();
    fireEvent.click(job.getByRole("button", { name: "Pause" }));
    await waitFor(() =>
      expect(within(screen.getByTestId("job-job-1")).getByText("paused")).toBeTruthy(),
    );
    fireEvent.click(
      within(screen.getByTestId("job-job-1")).getByRole("button", { name: "Resume" }),
    );
    await waitFor(() =>
      expect(mock.callsTo("POST", "/api/cron/jobs/job-1/resume")).toHaveLength(1),
    );
    fireEvent.click(
      within(screen.getByTestId("job-job-1")).getByRole("button", { name: "Run now" }),
    );
    await waitFor(() =>
      expect(mock.callsTo("POST", "/api/cron/jobs/job-1/trigger")).toHaveLength(1),
    );

    fireEvent.click(
      within(screen.getByTestId("job-job-1")).getByRole("button", { name: "Delete" }),
    );
    const dialog = within(await screen.findByRole("dialog"));
    expect(mock.callsTo("DELETE", "/api/cron/jobs/job-1")).toHaveLength(0);
    fireEvent.click(dialog.getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(screen.queryByTestId("job-job-1")).toBeNull());
  });

  it("edits a job in the same modal", async () => {
    mock.state.cronJobs = [
      {
        id: "job-1",
        name: "Digest",
        prompt: "Digest",
        schedule: "every 1d",
        schedule_display: "every 1d",
        deliver: "local",
        state: "scheduled",
        enabled: true,
        last_run_at: null,
        next_run_at: null,
        last_status: null,
      },
    ];
    renderPage(<CronPage />, "/cron");
    fireEvent.click(
      within(await screen.findByTestId("job-job-1")).getByRole("button", { name: "Edit" }),
    );
    const dialog = within(await screen.findByRole("dialog"));
    expect((dialog.getByLabelText("Prompt") as HTMLTextAreaElement).value).toBe("Digest");
    fireEvent.change(dialog.getByLabelText("Schedule"), { target: { value: "daily at 07:00" } });
    fireEvent.click(dialog.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(mock.callsTo("PUT", "/api/cron/jobs/job-1")).toHaveLength(1));
    expect(await screen.findByText("daily at 07:00")).toBeTruthy();
  });
});
