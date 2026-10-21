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
  await screen.findByText(/Version 0\.1\.0/);
}

describe("system page", () => {
  it("shows host numbers", async () => {
    renderPage(<SystemPage />, "/system");
    await ready();
    const host = section("This machine");
    await waitFor(() => expect(host.getByText("4")).toBeTruthy());
    expect(host.getByText("0.5 / 0.4 / 0.3")).toBeTruthy();
    expect(host.getByText("4.0 GiB of 8.0 GiB (50 %)")).toBeTruthy();
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
    const updates = section("Updates");
    expect(updates.getByText("3 commits behind")).toBeTruthy();
    fireEvent.click(updates.getByRole("button", { name: "Update" }));
    const dialog = within(await screen.findByRole("dialog"));
    expect(mock.state.actionsStarted).toEqual([]);
    fireEvent.click(dialog.getByRole("button", { name: "Update" }));
    await waitFor(() => expect(mock.state.actionsStarted).toEqual(["update"]));
    expect(await updates.findByTestId("action-log-update")).toBeTruthy();

    fireEvent.click(updates.getByRole("button", { name: "Check again" }));
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
    await screen.findByText(/installed as docker/);
    const updates = section("Updates");
    expect(updates.queryByRole("button", { name: "Update" })).toBeNull();
    expect(updates.getByText("Pull the newer image and recreate the container.")).toBeTruthy();
    expect(updateBadge(mock.state.update)).toBe("docker");
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
    const log = await daemon.findByTestId("action-log-daemon-stop");
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
  });

  it("offers the backup for download once it is written", async () => {
    renderPage(<SystemPage />, "/system");
    await ready();
    fireEvent.click(screen.getByRole("button", { name: "Back up this profile" }));
    expect(await screen.findByText("/home/user/.indus/backups/indus-backup-1.zip")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Download" })).toBeTruthy();
  });

  it("imports by path and refuses a relative one", async () => {
    renderPage(<SystemPage />, "/system");
    await ready();
    fireEvent.change(screen.getByLabelText("Import archive path"), {
      target: { value: "backup.zip" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Import" }));
    expect((await screen.findByRole("alert")).textContent).toContain("full path");
    fireEvent.change(screen.getByLabelText("Import archive path"), {
      target: { value: "/srv/backup.zip" },
    });
    fireEvent.click(screen.getByRole("checkbox", { name: "Overwrite existing files" }));
    fireEvent.click(screen.getByRole("button", { name: "Import" }));
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
    await waitFor(() => expect(hooks.getByLabelText("Command")).toBeTruthy());
    fireEvent.change(hooks.getByLabelText("Command"), { target: { value: "~/hooks/log.sh" } });
    const add = hooks.getByRole("button", { name: "Add hook" }) as HTMLButtonElement;
    expect(add.disabled).toBe(true);
    expect(hooks.getByText(/runs this command on your machine/)).toBeTruthy();
    fireEvent.click(hooks.getByRole("checkbox"));
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
    expect((hooks.getByRole("checkbox") as HTMLInputElement).checked).toBe(false);

    fireEvent.click(row.getByRole("button", { name: "Remove" }));
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
    expect(checkpoints.getByText("3.0 MiB", { selector: "span.rounded-full" })).toBeTruthy();
    fireEvent.change(checkpoints.getByLabelText("Keep days"), { target: { value: "3" } });
    fireEvent.click(checkpoints.getByRole("button", { name: "Prune checkpoints" }));
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
