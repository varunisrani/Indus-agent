// @vitest-environment jsdom
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { fakeRest } from "../../settings/fakeRest.ts";
import { markRestart, restartPending, type StorageLike } from "../manage/common.tsx";
import { MessagingPage } from "./MessagingPage.tsx";

const BOT_TOKEN = ["123456", "AAH", "fake", "telegram", "token"].join("-");

function memory(): StorageLike & { map: Map<string, string> } {
  const map = new Map<string, string>();
  return {
    map,
    getItem: (k) => map.get(k) ?? null,
    setItem: (k, v) => void map.set(k, v),
    removeItem: (k) => void map.delete(k),
  };
}

function platform(id: string, extra: Record<string, unknown> = {}) {
  return {
    id,
    name: id === "telegram" ? "Telegram" : "Slack",
    description: "",
    installed: true,
    install_hint: null,
    enabled: false,
    configured: false,
    state: "not_configured",
    reason: null,
    fields: [
      {
        key: id === "telegram" ? "TELEGRAM_BOT_TOKEN" : "SLACK_BOT_TOKEN",
        kind: "credential",
        required: true,
        is_set: id !== "telegram",
        is_secret: true,
        preview: id === "telegram" ? null : "…f00d",
        value: null,
      },
      {
        key: id === "telegram" ? "TELEGRAM_ALLOWED_USERS" : "SLACK_ALLOWED_USERS",
        kind: "policy",
        required: false,
        is_set: false,
        is_secret: false,
        preview: null,
        value: "",
      },
    ],
    ...extra,
  };
}

function backend() {
  const api = fakeRest();
  api.route("GET", "/api/messaging/platforms", {
    platforms: [platform("slack"), platform("telegram")],
    daemon_running: true,
  });
  api.route("PUT", "/api/messaging/platforms/slack", {
    ok: true,
    platform: "slack",
    restart_needed: true,
  });
  api.route("PUT", "/api/messaging/platforms/telegram", {
    ok: true,
    platform: "telegram",
    restart_needed: true,
  });
  api.route("POST", "/api/daemon/restart", { ok: true, action: "daemon-restart" });
  api.route("POST", "/api/pairing/approve", { ok: true });
  return api;
}

describe("restart mark", () => {
  it("the restart banner clears only when the instance id changes", () => {
    const store = memory();
    markRestart(store, "work", "inst-1");
    expect(restartPending(store, "work", "inst-1")).toBe(true); // a reconnect to the same daemon
    expect(restartPending(store, "work", null)).toBe(true); // disconnected: keep it
    expect(restartPending(store, "other", "inst-2")).toBe(false); // per profile
    expect(restartPending(store, "work", "inst-2")).toBe(false); // the daemon restarted
    expect(restartPending(store, "work", "inst-1")).toBe(false); // and the mark is gone
    markRestart(store, "work", null); // saved before any instance was known
    expect(restartPending(store, "work", "inst-3")).toBe(true); // adopts the first one seen
    expect(restartPending(store, "work", "inst-4")).toBe(false);
  });
});

describe("MessagingPage", () => {
  it("a card shows masked credentials and saves only changed fields", async () => {
    const api = backend();
    const store = memory();
    const view = render(
      <MessagingPage api={api} profile="work" instanceId="inst-1" storage={store} />,
    );
    const slack = await screen.findByRole("article", { name: "Slack" });
    const token = slack.querySelector("input[aria-label='SLACK_BOT_TOKEN']") as HTMLInputElement;
    expect(token.type).toBe("password");
    expect(token.value).toBe("");
    expect(token.placeholder).toContain("…f00d");
    fireEvent.change(
      slack.querySelector("input[aria-label='SLACK_ALLOWED_USERS']") as HTMLInputElement,
      {
        target: { value: "U1,U2" },
      },
    );
    fireEvent.click(slack.querySelector("input[type='checkbox']") as HTMLInputElement);
    fireEvent.click(within(slack).getByText("Save changes"));
    await waitFor(() => expect(api.calls.some((c) => c.method === "PUT")).toBe(true));
    const put = api.calls.find((c) => c.method === "PUT");
    expect(put).toMatchObject({
      path: "/api/messaging/platforms/slack",
      profile: "work",
      body: { enabled: true, env: { SLACK_ALLOWED_USERS: "U1,U2" } },
    });
    // the restart banner appears and stays across a reconnect to the same instance
    expect(await screen.findByText("Indus needs a restart to use the new settings.")).toBeTruthy();
    view.rerender(<MessagingPage api={api} profile="work" instanceId="inst-1" storage={store} />);
    expect(screen.queryByText("Indus needs a restart to use the new settings.")).toBeTruthy();
    fireEvent.click(screen.getByText("Restart now"));
    await waitFor(() => expect(api.calls.some((c) => c.path === "/api/daemon/restart")).toBe(true));
    expect(screen.queryByText("Indus needs a restart to use the new settings.")).toBeTruthy();
    view.rerender(<MessagingPage api={api} profile="work" instanceId="inst-2" storage={store} />);
    expect(screen.queryByText("Indus needs a restart to use the new settings.")).toBeNull();
  });

  it("telegram quick setup detects the user id from the first message", async () => {
    const api = backend();
    let pairingCalls = 0;
    api.route("GET", "/api/pairing", () => {
      pairingCalls += 1;
      return {
        status: 200,
        body:
          pairingCalls < 2
            ? { pending: [], approved: [] }
            : {
                pending: [
                  {
                    platform: "slack",
                    request_id: "x",
                    user_id: "U9",
                    user_name: null,
                    age_seconds: 1,
                  },
                  {
                    platform: "telegram",
                    request_id: "req-7",
                    user_id: "42424242",
                    user_name: "Asha",
                    age_seconds: 3,
                  },
                ],
                approved: [],
              },
      };
    });
    const opened: string[] = [];
    render(
      <MessagingPage
        api={api}
        profile="work"
        instanceId="inst-1"
        storage={memory()}
        openExternal={(url) => opened.push(url)}
        pollMs={5}
      />,
    );
    // W52-k: one platform's detail at a time (the first is chosen); pick Telegram in the rail
    fireEvent.click(await screen.findByRole("button", { name: "Telegram" }));
    fireEvent.click(await screen.findByText("Create a bot in Telegram"));
    expect(opened).toEqual(["https://t.me/BotFather"]);
    fireEvent.change(screen.getByLabelText("Bot token"), { target: { value: BOT_TOKEN } });
    fireEvent.change(screen.getByLabelText("Bot name (optional)"), {
      target: { value: "@asha_helper_bot" },
    });
    fireEvent.click(screen.getByText("Save and restart"));
    expect(await screen.findByText("https://t.me/asha_helper_bot")).toBeTruthy();
    const input = (await screen.findByLabelText("Your Telegram user id")) as HTMLInputElement;
    expect(input.value).toBe("42424242");
    expect(screen.getByText("A message arrived from Asha.")).toBeTruthy();
    fireEvent.click(screen.getByText("Allow and finish"));
    expect(await screen.findByText("Done. Your bot answers you now.")).toBeTruthy();
    const puts = api.calls.filter(
      (c) => c.method === "PUT" && c.path === "/api/messaging/platforms/telegram",
    );
    expect(puts.map((c) => c.body)).toEqual([
      { enabled: true, env: { TELEGRAM_BOT_TOKEN: BOT_TOKEN } },
      { env: { TELEGRAM_ALLOWED_USERS: "42424242" } },
    ]);
    expect(api.calls.find((c) => c.path === "/api/pairing/approve")?.body).toEqual({
      platform: "telegram",
      request_id: "req-7",
    });
    expect(api.calls.filter((c) => c.path === "/api/daemon/restart")).toHaveLength(2);
    // the token was typed once and is not left in the page
    expect(document.body.innerHTML).not.toContain(BOT_TOKEN);
  });

  it("telegram quick setup needs a token and times out after ten minutes", async () => {
    const api = backend();
    api.route("GET", "/api/pairing", { pending: [], approved: [] });
    render(
      <MessagingPage api={api} profile={null} instanceId={null} storage={memory()} pollMs={5} />,
    );
    fireEvent.click(await screen.findByRole("button", { name: "Telegram" }));
    fireEvent.click(await screen.findByText("I already have a token"));
    fireEvent.click(screen.getByText("Save and restart"));
    expect(await screen.findByText("Paste the token BotFather gave you.")).toBeTruthy();
    const realNow = Date.now;
    let offset = 0;
    Date.now = () => realNow() + offset;
    try {
      fireEvent.change(screen.getByLabelText("Bot token"), { target: { value: BOT_TOKEN } });
      fireEvent.click(screen.getByText("Save and restart"));
      await screen.findByText(/Now send your bot any message/);
      await act(async () => {
        offset = 10 * 60_000 + 1;
        await new Promise((resolve) => setTimeout(resolve, 30));
      });
      expect(
        await screen.findByText("No message arrived within ten minutes. Try again."),
      ).toBeTruthy();
    } finally {
      Date.now = realNow;
    }
  });
});
