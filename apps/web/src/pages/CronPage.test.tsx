import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createMockApi, isValidSchedule, type MockApi } from "../testing/mockApi.ts";
import { renderPage } from "../testing/render.tsx";
import { CronPage } from "./CronPage.tsx";
import {
  DEFAULT_DRAFT,
  draftProblem,
  draftToSchedule,
  scheduleToDraft,
} from "./cron/scheduleBuilder.ts";

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
  fireEvent.click(await screen.findByRole("button", { name: "Create" }));
  return within(await screen.findByRole("dialog"));
}

describe("schedule builder", () => {
  it("emits our grammar strings, which the store accepts", () => {
    const every = draftToSchedule({ ...DEFAULT_DRAFT, mode: "interval", every: 30, unit: "m" });
    const daily = draftToSchedule({ ...DEFAULT_DRAFT, mode: "daily", time: "09:30" });
    const weekly = draftToSchedule({
      ...DEFAULT_DRAFT,
      mode: "weekly",
      days: [5, 1, 3],
      time: "18:05",
    });
    const monthly = draftToSchedule({
      ...DEFAULT_DRAFT,
      mode: "monthly",
      dayOfMonth: 15,
      time: "07:00",
    });
    const once = draftToSchedule({ ...DEFAULT_DRAFT, mode: "once", at: "2026-11-02T08:30" });
    const custom = draftToSchedule({ ...DEFAULT_DRAFT, mode: "custom", custom: " in  2h " });
    expect([every, daily, weekly, monthly, once, custom]).toEqual([
      "every 30m",
      "daily at 09:30",
      "5 18 * * 1,3,5",
      "0 7 15 * *",
      "2026-11-02T08:30",
      "in 2h",
    ]);
    for (const schedule of [every, daily, weekly, monthly, once, custom])
      expect(isValidSchedule(schedule)).toBe(true);
  });

  it("reads schedules back into the picker, unfamiliar ones as custom", () => {
    expect(scheduleToDraft("every 2h")).toMatchObject({ mode: "interval", every: 2, unit: "h" });
    expect(scheduleToDraft("daily at 8:00")).toMatchObject({ mode: "daily", time: "08:00" });
    expect(scheduleToDraft("5 18 * * 1,3,5")).toMatchObject({
      mode: "weekly",
      time: "18:05",
      days: [1, 3, 5],
    });
    expect(scheduleToDraft("0 7 15 * *")).toMatchObject({ mode: "monthly", dayOfMonth: 15 });
    expect(scheduleToDraft("2026-11-02T08:30")).toMatchObject({ mode: "once" });
    expect(scheduleToDraft("weekdays at 18:00")).toMatchObject({
      mode: "custom",
      custom: "weekdays at 18:00",
    });
  });

  it("refuses drafts that cannot become a schedule", () => {
    expect(draftProblem({ ...DEFAULT_DRAFT, mode: "interval", every: 0 })).not.toBeNull();
    expect(draftProblem({ ...DEFAULT_DRAFT, mode: "interval", every: 1.5 })).not.toBeNull();
    expect(draftProblem({ ...DEFAULT_DRAFT, mode: "daily", time: "25:00" })).not.toBeNull();
    expect(draftProblem({ ...DEFAULT_DRAFT, mode: "weekly", days: [] })).not.toBeNull();
    expect(draftProblem({ ...DEFAULT_DRAFT, mode: "monthly", dayOfMonth: 31 })).not.toBeNull();
    expect(draftProblem({ ...DEFAULT_DRAFT, mode: "once", at: "" })).not.toBeNull();
    expect(draftProblem({ ...DEFAULT_DRAFT, mode: "custom", custom: " " })).not.toBeNull();
    expect(isValidSchedule("whenever")).toBe(false);
  });
});

describe("cron page", () => {
  it("creates a job with the builder's every 30m", async () => {
    const dialog = await openNewJob();
    fireEvent.change(dialog.getByLabelText("Prompt"), { target: { value: "Summarise my inbox" } });
    expect((dialog.getByLabelText("Schedule") as HTMLSelectElement).value).toBe("interval");
    expect(dialog.getByText("every 30m")).toBeTruthy();
    await dialog.findByRole("option", { name: "Telegram home chat" });
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

  it("creates a one-shot job at a date and time", async () => {
    const dialog = await openNewJob();
    fireEvent.change(dialog.getByLabelText("Prompt"), { target: { value: "Remind me" } });
    fireEvent.change(dialog.getByLabelText("Schedule"), { target: { value: "once" } });
    fireEvent.change(dialog.getByLabelText("Run at"), { target: { value: "2026-11-02T08:30" } });
    expect(dialog.getByText("2026-11-02T08:30")).toBeTruthy();
    fireEvent.click(dialog.getByRole("button", { name: "Create" }));
    await waitFor(() => expect(mock.state.cronJobs).toHaveLength(1));
    expect(mock.state.cronJobs[0]?.schedule).toBe("2026-11-02T08:30");
  });

  it("creates a weekly job with skills ticked", async () => {
    mock.state.skills = [
      { name: "digest", description: "Digests", category: "", enabled: true },
      { name: "other", description: "Other", category: "", enabled: true },
    ];
    const dialog = await openNewJob();
    fireEvent.change(dialog.getByLabelText("Prompt"), { target: { value: "Weekly review" } });
    fireEvent.change(dialog.getByLabelText("Schedule"), { target: { value: "weekly" } });
    fireEvent.click(dialog.getByRole("button", { name: "Tue" }));
    fireEvent.change(dialog.getByLabelText("Time of day"), { target: { value: "17:45" } });
    fireEvent.click(await dialog.findByRole("checkbox", { name: "digest" }));
    fireEvent.click(dialog.getByRole("button", { name: "Create" }));
    await waitFor(() => expect(mock.callsTo("POST", "/api/cron/jobs")).toHaveLength(1));
    expect(mock.callsTo("POST", "/api/cron/jobs")[0]?.body).toMatchObject({
      schedule: "45 17 * * 1,3,4,5",
      skills: ["digest"],
    });
  });

  it("keeps the modal open with the server's reason for a bad schedule", async () => {
    const dialog = await openNewJob();
    fireEvent.change(dialog.getByLabelText("Prompt"), { target: { value: "Do it" } });
    fireEvent.change(dialog.getByLabelText("Schedule"), { target: { value: "custom" } });
    fireEvent.change(dialog.getByLabelText("Custom schedule"), { target: { value: "whenever" } });
    fireEvent.click(dialog.getByRole("button", { name: "Create" }));
    expect((await dialog.findByRole("alert")).textContent).toContain(
      "'whenever' is not a schedule",
    );
    expect(screen.getByRole("dialog")).toBeTruthy();
  });

  it("marks delivery targets that are not set up", async () => {
    const dialog = await openNewJob();
    const email = (await within(dialog.getByLabelText("Deliver to")).findByRole("option", {
      name: /Email/,
    })) as HTMLOptionElement;
    expect(email.disabled).toBe(true);
    expect(email.textContent).toContain("set up first");
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
    expect(dialog.queryByLabelText("Profile")).toBeNull();
    expect(dialog.getByText("job-1")).toBeTruthy();
    expect((dialog.getByLabelText("Every") as HTMLInputElement).value).toBe("1");
    fireEvent.change(dialog.getByLabelText("Schedule"), { target: { value: "daily" } });
    fireEvent.change(dialog.getByLabelText("Time of day"), { target: { value: "07:00" } });
    fireEvent.click(dialog.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(mock.callsTo("PUT", "/api/cron/jobs/job-1")).toHaveLength(1));
    expect(await screen.findByText("daily at 07:00")).toBeTruthy();
  });

  it("asks each profile for All profiles and tags rows with it", async () => {
    mock.state.profiles = {
      profiles: [
        { name: "default", is_default: true },
        { name: "work", is_default: false },
      ],
      active: "default",
    };
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
    expect(await screen.findAllByTestId("job-job-1")).toHaveLength(2);
    const asked = mock.callsTo("GET", "/api/cron/jobs").map((c) => c.query.get("profile"));
    expect(asked).toEqual(["default", "work"]);
    expect(screen.getByText("Scheduled Jobs (2)")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Profile"), { target: { value: "work" } });
    await waitFor(() => expect(screen.getAllByTestId("job-job-1")).toHaveLength(1));
    expect(within(screen.getByTestId("job-job-1")).getByText("work")).toBeTruthy();
  });

  it("prefills the job panel from a blueprint", async () => {
    mock.state.cronBlueprints = [
      {
        key: "weekly-review",
        title: "Weekly review",
        source: "catalogue",
        skill: null,
        description: "Looks back over the week",
        schedule: "30 16 * * 5",
        prompt: "Review the week",
        fields: [],
      },
      {
        key: "inbox-digest:blueprint",
        title: "inbox-digest",
        source: "skill",
        skill: "inbox-digest",
        description: "Sum up mail",
        schedule: "daily at 08:00",
        prompt: "Sum up my mail",
        fields: [],
      },
    ];
    renderPage(<CronPage />, "/cron");
    fireEvent.click(await screen.findByRole("button", { name: "Blueprints" }));
    const starter = within(await screen.findByTestId("blueprint-weekly-review"));
    expect(starter.getByText("Weekly review")).toBeTruthy();
    expect(starter.getByText("Starter")).toBeTruthy();
    const card = within(await screen.findByTestId("blueprint-inbox-digest:blueprint"));
    fireEvent.click(card.getByRole("button", { name: "Set up" }));
    const dialog = within(await screen.findByRole("dialog"));
    expect((dialog.getByLabelText("Prompt") as HTMLTextAreaElement).value).toBe("Sum up my mail");
    expect((dialog.getByLabelText("Schedule") as HTMLSelectElement).value).toBe("daily");
  });
});
