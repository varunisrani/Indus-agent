import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MessagingPlatform } from "../lib/restTypes.ts";
import { createMockApi, type MockApi } from "../testing/mockApi.ts";
import { renderPage } from "../testing/render.tsx";
import { ACTION_POLL_MS } from "../ui/ActionLog.tsx";
import { ChannelsPage, changedValues, DAEMON_POLL_MS, readRestartMark } from "./ChannelsPage.tsx";

let mock: MockApi;
let restore: () => void;

function telegram(): MessagingPlatform {
  return {
    id: "telegram",
    name: "Telegram",
    description: "A bot in Telegram chats",
    installed: true,
    install_hint: null,
    enabled: true,
    explicitly_disabled: false,
    configured: true,
    state: "connected",
    reason: null,
    daemon_running: true,
    fields: [
      {
        key: "TELEGRAM_BOT_TOKEN",
        kind: "credential",
        required: true,
        is_set: true,
        is_secret: true,
        preview: "1234…ab",
        value: null,
      },
      {
        key: "TELEGRAM_ALLOWED_USERS",
        kind: "policy",
        required: false,
        is_set: true,
        is_secret: false,
        preview: null,
        value: "42",
      },
      {
        key: "TELEGRAM_HOME_CHANNEL",
        kind: "policy",
        required: false,
        is_set: false,
        is_secret: false,
        preview: null,
        value: null,
      },
    ],
  };
}

beforeEach(() => {
  mock = createMockApi();
  mock.state.platforms = [telegram()];
  restore = mock.install();
  vi.spyOn(console, "warn").mockImplementation(() => {});
});

afterEach(() => {
  restore();
  vi.useRealTimers();
});

async function card() {
  return within(await screen.findByTestId("platform-telegram"));
}

/** The platform's fields live in the Configure dialog opened from its row. */
async function configure() {
  const row = await card();
  fireEvent.click(row.getByRole("button", { name: "Configure" }));
  return within(await screen.findByRole("dialog", { name: "Configure Telegram" }));
}

describe("channels page", () => {
  it("starts every credential input empty and shows only the masked preview", async () => {
    renderPage(<ChannelsPage />, "/channels");
    expect((await card()).getByText("Connected")).toBeTruthy();
    const dialog = await configure();
    const token = dialog.getByLabelText(/^TELEGRAM_BOT_TOKEN/) as HTMLInputElement;
    expect(token.type).toBe("password");
    expect(token.value).toBe("");
    expect(dialog.getByText(/Stored \(1234…ab\)\. Leave blank to keep it\./)).toBeTruthy();
    // Settings are not secrets and show their value.
    expect((dialog.getByLabelText(/^TELEGRAM_ALLOWED_USERS/) as HTMLInputElement).value).toBe("42");
  });

  it("sends only the fields that were typed into", async () => {
    renderPage(<ChannelsPage />, "/channels");
    const dialog = await configure();
    fireEvent.change(dialog.getByLabelText(/^TELEGRAM_HOME_CHANNEL/), {
      target: { value: "-100200" },
    });
    fireEvent.click(dialog.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(mock.callsTo("PUT", "/api/messaging/platforms/telegram")).toHaveLength(1),
    );
    // The blank token and the unchanged allow-list are not sent: blank keeps what is stored.
    expect(mock.callsTo("PUT", "/api/messaging/platforms/telegram")[0]?.body).toEqual({
      env: { TELEGRAM_HOME_CHANNEL: "-100200" },
    });
  });

  it("clears the typed credential after a save and never shows it", async () => {
    renderPage(<ChannelsPage />, "/channels");
    const value = ["typed", "bot", "value"].join("-");
    const dialog = await configure();
    const token = dialog.getByLabelText(/^TELEGRAM_BOT_TOKEN/) as HTMLInputElement;
    fireEvent.change(token, { target: { value } });
    fireEvent.click(dialog.getByRole("button", { name: "Save" }));
    // A successful save closes the dialog; opened again, the credential input starts empty.
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    const again = await configure();
    expect((again.getByLabelText(/^TELEGRAM_BOT_TOKEN/) as HTMLInputElement).value).toBe("");
    expect(mock.callsTo("PUT", "/api/messaging/platforms/telegram")[0]?.body).toEqual({
      env: { TELEGRAM_BOT_TOKEN: value },
    });
    expect(document.body.textContent).not.toContain(value);
  });

  it("refuses an empty save without a request", async () => {
    renderPage(<ChannelsPage />, "/channels");
    const dialog = await configure();
    fireEvent.click(dialog.getByRole("button", { name: "Save" }));
    expect((await dialog.findByRole("alert")).textContent).toContain("Nothing to save");
    expect(mock.callsTo("PUT", "/api/messaging/platforms/telegram")).toHaveLength(0);
  });

  it("clears a stored field and switches the platform", async () => {
    renderPage(<ChannelsPage />, "/channels");
    const dialog = await configure();
    fireEvent.click(dialog.getByRole("button", { name: "Clear TELEGRAM_ALLOWED_USERS" }));
    await waitFor(() =>
      expect(mock.callsTo("PUT", "/api/messaging/platforms/telegram")[0]?.body).toEqual({
        clear_env: ["TELEGRAM_ALLOWED_USERS"],
      }),
    );
    // Clearing keeps the dialog open; Cancel closes it without sending anything else.
    fireEvent.click(dialog.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    const row = await card();
    fireEvent.click(row.getByRole("switch", { name: "Enable Telegram" }));
    await waitFor(() =>
      expect(mock.callsTo("PUT", "/api/messaging/platforms/telegram")[1]?.body).toEqual({
        enabled: false,
      }),
    );
  });

  it("shows the server's test message", async () => {
    mock.state.platformTests.telegram = {
      ok: false,
      state: "daemon_stopped",
      message: "The daemon is not running. Start it to connect this platform.",
    };
    renderPage(<ChannelsPage />, "/channels");
    const row = await card();
    fireEvent.click(row.getByRole("button", { name: "Test" }));
    expect((await row.findByTestId("test-result-telegram")).textContent).toBe(
      "The daemon is not running. Start it to connect this platform.",
    );
  });

  it("keeps the restart banner until the daemon reports a new process", async () => {
    mock.state.actionScripts["daemon-restart"] = [
      {
        name: "daemon-restart",
        state: "running",
        running: true,
        exit_code: null,
        lines: ["stop requested; waiting for the daemon to finish"],
      },
      {
        name: "daemon-restart",
        state: "done",
        running: false,
        exit_code: 0,
        lines: ["stop requested; waiting for the daemon to finish", "daemon: ready"],
      },
    ];
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    renderPage(<ChannelsPage />, "/channels");
    await card();
    await waitFor(() => expect(mock.callsTo("GET", "/api/daemon").length).toBeGreaterThan(0));
    const dialog = await configure();
    fireEvent.change(dialog.getByLabelText(/^TELEGRAM_HOME_CHANNEL/), { target: { value: "7" } });
    fireEvent.click(dialog.getByRole("button", { name: "Save" }));
    const banner = await screen.findByTestId("restart-banner");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(readRestartMark("")).toEqual({ pid: 4242 });

    // Pressing restart does not clear it; the action's log is tailed.
    fireEvent.click(within(banner).getByRole("button", { name: "Restart daemon" }));
    await waitFor(() => expect(mock.state.actionsStarted).toEqual(["daemon-restart"]));
    const log = await screen.findByTestId("action-log-daemon-restart");
    await waitFor(() => expect(log.textContent).toContain("waiting for the daemon"));
    act(() => {
      vi.advanceTimersByTime(ACTION_POLL_MS);
    });
    await waitFor(() => expect(log.textContent).toContain("daemon: ready"));

    // A status poll that still sees the same process keeps it.
    act(() => {
      vi.advanceTimersByTime(DAEMON_POLL_MS);
    });
    await waitFor(() => expect(mock.callsTo("GET", "/api/daemon").length).toBeGreaterThan(1));
    expect(screen.getByTestId("restart-banner")).toBeTruthy();

    // The daemon reports a new process: the banner goes, and so does the stored mark.
    mock.state.daemon = { ...mock.state.daemon, pid: 5151 };
    act(() => {
      vi.advanceTimersByTime(DAEMON_POLL_MS);
    });
    await waitFor(() => expect(screen.queryByTestId("restart-banner")).toBeNull());
    expect(readRestartMark("")).toBeNull();
  });

  it("keeps the banner across a reload of the page", async () => {
    const first = renderPage(<ChannelsPage />, "/channels");
    const row = await card();
    await waitFor(() => expect(mock.callsTo("GET", "/api/daemon").length).toBeGreaterThan(0));
    fireEvent.click(row.getByRole("switch", { name: "Enable Telegram" }));
    await screen.findByTestId("restart-banner");
    first.unmount();

    renderPage(<ChannelsPage />, "/channels");
    expect(await screen.findByTestId("restart-banner")).toBeTruthy();
  });

  it("works out which values a save sends", () => {
    const fields = telegram().fields;
    expect(
      changedValues(fields, {
        TELEGRAM_BOT_TOKEN: "   ",
        TELEGRAM_ALLOWED_USERS: "42",
        TELEGRAM_HOME_CHANNEL: " 9 ",
      }),
    ).toEqual({ TELEGRAM_HOME_CHANNEL: "9" });
  });
});
