import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ActionStatus } from "../lib/restTypes.ts";
import { createMockApi, type MockApi } from "../testing/mockApi.ts";
import { renderPage } from "../testing/render.tsx";
import { ACTION_POLL_MS } from "../ui/ActionLog.tsx";
import { SystemPage } from "./SystemPage.tsx";
import { updateBadge } from "./system/MachineSections.tsx";

let mock: MockApi;
let restore: () => void;

function run(name: string, lines: string[], done = false): ActionStatus {
  return {
    name,
    state: done ? "done" : "running",
    running: !done,
    exit_code: done ? 0 : null,
    lines,
  };
}

beforeEach(() => {
  mock = createMockApi();
  restore = mock.install();
  vi.spyOn(console, "warn").mockImplementation(() => {});
});

afterEach(() => {
  restore();
  vi.useRealTimers();
});

function section(name: string) {
  return within(screen.getByRole("heading", { name }).closest("section") as HTMLElement);
}

async function ready() {
  await screen.findByText("v0.1.0");
  await waitFor(() => expect(screen.queryByLabelText("Loading")).toBeNull());
}

describe("system page", () => {
  it("shows host numbers in tiles and leaves out the ones we do not have", async () => {
    renderPage(<SystemPage />, "/system");
    await ready();
    const host = section("Host");
    expect(host.getByText("4 cores")).toBeTruthy();
    expect(host.getByText("0.50 / 0.40 / 0.30")).toBeTruthy();
    expect(host.getByText("4.0 GB / 8.0 GB (50.0%)")).toBeTruthy();
    expect(host.getByText("40.0 GB / 100.0 GB (40.0%)")).toBeTruthy();
    expect(host.getByText("latest")).toBeTruthy();
    expect(host.queryByText("Uptime")).toBeNull();
    expect(host.getByText("Up to date.")).toBeTruthy();
  });

  it("shows one section's load failure with a Retry that refetches", async () => {
    mock.failNext("GET", "/api/memory", {
      status: 500,
      body: { detail: { code: "internal", message: "store offline" } },
    });
    renderPage(<SystemPage />, "/system");
    await ready();
    const memory = section("Memory");
    expect((await memory.findByRole("alert")).textContent).toContain(
      "Could not load the memory settings",
    );
    expect(memory.getByText("Details: store offline")).toBeTruthy();
    const before = mock.callsTo("GET", "/api/memory").length;
    fireEvent.click(memory.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(mock.callsTo("GET", "/api/memory").length).toBe(before + 1));
    // the other sections still render
    expect(section("Host").getByText("4 cores")).toBeTruthy();
  });

  it("badges commits behind and asks before updating", async () => {
    mock.state.update = {
      ...mock.state.update,
      behind: 3,
      update_available: true,
      message: "3 commit(s) behind.",
    };
    renderPage(<SystemPage />, "/system");
    await ready();
    const updates = section("Host");
    expect(updates.getByText("3 behind")).toBeTruthy();
    fireEvent.click(updates.getByRole("button", { name: "Update now" }));
    const dialog = within(await screen.findByRole("dialog"));
    expect(mock.state.actionsStarted).toEqual([]);
    fireEvent.click(dialog.getByRole("button", { name: "Update" }));
    await waitFor(() => expect(mock.state.actionsStarted).toEqual(["update"]));
    expect(await screen.findByTestId("action-log-update")).toBeTruthy();

    fireEvent.click(updates.getByRole("button", { name: "Check for updates" }));
    await waitFor(() =>
      expect(
        mock.callsTo("GET", "/api/system/update").some((c) => c.query.get("force") === "1"),
      ).toBe(true),
    );
  });

  it("offers no update button for an install that is not a git checkout", async () => {
    mock.state.update = {
      ...mock.state.update,
      install_method: "docker",
      can_apply: false,
      behind: null,
      message: "Pull the newer image and recreate the container.",
    };
    renderPage(<SystemPage />, "/system");
    await ready();
    const updates = section("Host");
    expect(updates.queryByRole("button", { name: "Update now" })).toBeNull();
    expect(updates.getByText("Pull the newer image and recreate the container.")).toBeTruthy();
    expect(updateBadge(mock.state.update)).toBe("");
  });

  it("asks before stopping the daemon, then tails the stop", async () => {
    mock.state.actionScripts["daemon-stop"] = [
      run("daemon-stop", ["stop requested; waiting for the daemon to finish"]),
      run(
        "daemon-stop",
        ["stop requested; waiting for the daemon to finish", "the daemon stopped"],
        true,
      ),
    ];
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    renderPage(<SystemPage />, "/system");
    await ready();
    const daemon = section("Daemon");
    await waitFor(() => expect(daemon.getByText("Process 4242")).toBeTruthy());
    fireEvent.click(daemon.getByRole("button", { name: "Stop" }));
    const dialog = within(await screen.findByRole("dialog"));
    expect(mock.state.actionsStarted).toEqual([]);
    fireEvent.click(dialog.getByRole("button", { name: "Stop" }));
    const log = await screen.findByTestId("action-log-daemon-stop");
    await waitFor(() => expect(log.textContent).toContain("waiting for the daemon"));
    act(() => {
      vi.advanceTimersByTime(ACTION_POLL_MS);
    });
    await waitFor(() => expect(log.textContent).toContain("the daemon stopped"));
    expect(mock.state.actionsStarted).toEqual(["daemon-stop"]);
  });

  it("tails the doctor log and stops when it is done", async () => {
    mock.state.actionScripts.doctor = [
      run("doctor", ["checking the config"]),
      run("doctor", ["checking the config", "checking the store"]),
      run("doctor", ["checking the config", "checking the store", "all good"], true),
    ];
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    renderPage(<SystemPage />, "/system");
    await ready();
    fireEvent.click(screen.getByRole("button", { name: "Run doctor" }));
    const log = await screen.findByTestId("action-log-doctor");
    await waitFor(() => expect(log.textContent).toBe("checking the config"));
    act(() => {
      vi.advanceTimersByTime(ACTION_POLL_MS);
    });
    await waitFor(() => expect(log.textContent).toBe("checking the config\nchecking the store"));
    act(() => {
      vi.advanceTimersByTime(ACTION_POLL_MS);
    });
    await waitFor(() => expect(log.textContent).toContain("all good"));
    const polls = mock.callsTo("GET", "/api/actions/doctor/status").length;
    act(() => {
      vi.advanceTimersByTime(ACTION_POLL_MS * 5);
    });
    expect(mock.callsTo("GET", "/api/actions/doctor/status")).toHaveLength(polls);
    expect(screen.getByText("Exit code 0.")).toBeTruthy();
    expect(screen.getByText("done")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Close log" }));
    expect(screen.queryByTestId("action-log-doctor")).toBeNull();
  });

  it("offers the backup for download once it is written", async () => {
    renderPage(<SystemPage />, "/system");
    await ready();
    const download = screen.getByRole("button", { name: "Download backup" }) as HTMLButtonElement;
    expect(download.disabled).toBe(true);
    expect(screen.getByText("No backup created yet")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Create backup" }));
    expect(await screen.findByText("/home/user/.indus/backups/indus-backup-1.zip")).toBeTruthy();
    expect(download.disabled).toBe(false);
  });

  it("restores by path after confirming and refuses a relative one", async () => {
    renderPage(<SystemPage />, "/system");
    await ready();
    fireEvent.change(screen.getByLabelText("Restore from path"), {
      target: { value: "backup.zip" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Restore path" }));
    expect((await screen.findByRole("alert")).textContent).toContain("full path");
    fireEvent.change(screen.getByLabelText("Restore from path"), {
      target: { value: "/srv/backup.zip" },
    });
    fireEvent.click(screen.getByRole("checkbox", { name: "Overwrite existing files" }));
    fireEvent.click(screen.getByRole("button", { name: "Restore path" }));
    const dialog = within(await screen.findByRole("dialog"));
    expect(mock.callsTo("POST", "/api/ops/import")).toHaveLength(0);
    fireEvent.click(dialog.getByRole("button", { name: "Restore" }));
    await waitFor(() =>
      expect(mock.callsTo("POST", "/api/ops/import")[0]?.body).toEqual({
        archive: "/srv/backup.zip",
        force: true,
      }),
    );
  });

  it("adds a pool key, clears the input and shows only the preview", async () => {
    renderPage(<SystemPage />, "/system");
    await ready();
    const pool = section("Credential pool");
    const value = ["pool", "value", "123456789"].join("-");
    fireEvent.change(pool.getByLabelText("Provider"), { target: { value: "fake" } });
    const key = pool.getByLabelText("API key") as HTMLInputElement;
    expect(key.type).toBe("password");
    fireEvent.change(key, { target: { value } });
    fireEvent.click(pool.getByRole("button", { name: "Add key" }));
    await waitFor(() => expect(pool.queryByText("No pooled credentials yet.")).toBeNull());
    const row = within(await pool.findByTestId("pool-fake-1"));
    expect(key.value).toBe("");
    expect(row.getByText("pool…89")).toBeTruthy();
    expect(document.body.textContent).not.toContain(value);
    expect(mock.callsTo("POST", "/api/credentials/pool")[0]?.body).toEqual({
      provider: "fake",
      api_key: value,
    });

    fireEvent.click(row.getByRole("button", { name: "Remove api-key-1" }));
    const dialog = within(await screen.findByRole("dialog"));
    fireEvent.click(dialog.getByRole("button", { name: "Remove" }));
    await waitFor(() => expect(pool.queryByTestId("pool-fake-1")).toBeNull());
    expect(mock.callsTo("DELETE", "/api/credentials/pool/fake/1")).toHaveLength(1);
  });

  it("selects a memory provider and resets to built-in", async () => {
    mock.state.memory = { ...mock.state.memory, providers: ["recall"] };
    renderPage(<SystemPage />, "/system");
    await ready();
    const memory = section("Memory");
    await waitFor(() => expect(memory.getByRole("option", { name: "recall" })).toBeTruthy());
    fireEvent.change(memory.getByLabelText("Memory provider"), { target: { value: "recall" } });
    fireEvent.click(memory.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(mock.state.memory.active).toBe("recall"));
    await waitFor(() =>
      expect(
        (memory.getByRole("button", { name: "Reset to built-in" }) as HTMLButtonElement).disabled,
      ).toBe(false),
    );
    fireEvent.click(memory.getByRole("button", { name: "Reset to built-in" }));
    await waitFor(() =>
      expect(mock.callsTo("PUT", "/api/memory/provider").at(-1)?.body).toEqual({ provider: "" }),
    );
  });

  it("adds a shell hook only with consent, and removes one after confirming", async () => {
    renderPage(<SystemPage />, "/system");
    await ready();
    const hooks = section("Shell hooks");
    expect(hooks.getByText("No shell hooks.")).toBeTruthy();
    fireEvent.click(hooks.getByRole("button", { name: "New hook" }));
    const modal = within(await screen.findByRole("dialog", { name: "New shell hook" }));
    fireEvent.change(modal.getByLabelText("Command"), { target: { value: "~/hooks/log.sh" } });
    const add = modal.getByRole("button", { name: "Add hook" }) as HTMLButtonElement;
    expect(add.disabled).toBe(true);
    expect(modal.getByText(/runs this command on your machine/)).toBeTruthy();
    fireEvent.click(modal.getByRole("checkbox"));
    expect(add.disabled).toBe(false);
    fireEvent.click(add);
    await waitFor(() =>
      expect(mock.callsTo("POST", "/api/ops/hooks")[0]?.body).toEqual({
        event: "pre_tool_call",
        command: "~/hooks/log.sh",
        consent: true,
      }),
    );
    const row = within(await hooks.findByTestId("hook-grant-1"));
    expect(row.getByText("approved")).toBeTruthy();
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    fireEvent.click(row.getByRole("button", { name: "Remove hook" }));
    const dialog = within(await screen.findByRole("dialog"));
    fireEvent.click(dialog.getByRole("button", { name: "Remove" }));
    await waitFor(() => expect(hooks.queryByTestId("hook-grant-1")).toBeNull());
    expect(mock.callsTo("DELETE", "/api/ops/hooks")[0]?.body).toEqual({
      event: "pre_tool_call",
      command: "~/hooks/log.sh",
    });
  });

  it("lists checkpoints and prunes with the chosen rules", async () => {
    mock.state.checkpoints = {
      total_bytes: 3 * 1024 * 1024,
      projects: [
        {
          key: "k1",
          workdir: "/srv/app",
          commits: 4,
          bytes: 3 * 1024 * 1024,
          last_touch: null,
          exists: true,
        },
      ],
    };
    renderPage(<SystemPage />, "/system");
    await ready();
    const checkpoints = section("Checkpoints");
    await waitFor(() => expect(checkpoints.getByText("/srv/app")).toBeTruthy());
    expect(checkpoints.getByText("1 project · 3.0 MB")).toBeTruthy();
    fireEvent.change(checkpoints.getByLabelText("Keep days"), { target: { value: "3" } });
    fireEvent.click(checkpoints.getByRole("button", { name: "Prune checkpoints" }));
    const dialog = within(await screen.findByRole("dialog"));
    expect(mock.callsTo("POST", "/api/ops/checkpoints/prune")).toHaveLength(0);
    fireEvent.click(dialog.getByRole("button", { name: "Prune" }));
    await waitFor(() =>
      expect(mock.callsTo("POST", "/api/ops/checkpoints/prune")[0]?.body).toEqual({
        retention_days: 3,
        max_size_mb: 500,
      }),
    );
    fireEvent.change(checkpoints.getByLabelText("Keep days"), { target: { value: "x" } });
    expect(
      (checkpoints.getByRole("button", { name: "Prune checkpoints" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
  });
});
