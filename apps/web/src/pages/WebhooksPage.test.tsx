import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { WebhookRoute } from "../lib/restTypes.ts";
import { createMockApi, type MockApi } from "../testing/mockApi.ts";
import { renderPage } from "../testing/render.tsx";
import { renderApp } from "../testing/renderApp.tsx";
import { parseEvents, WebhooksPage } from "./WebhooksPage.tsx";

let mock: MockApi;
let restore: () => void;

function route(name: string, extra: Partial<WebhookRoute> = {}): WebhookRoute {
  return {
    name,
    source: "subscribed",
    url: `http://127.0.0.1:8644/webhooks/${name}`,
    auth: "auto",
    events: [],
    prompt: "",
    deliver: null,
    deliver_only: false,
    description: "",
    created_at: null,
    enabled: true,
    secret_set: true,
    ...extra,
  };
}

beforeEach(() => {
  mock = createMockApi();
  mock.state.webhooks = [
    route("deploys", { events: ["push"], deliver: "telegram:42" }),
    route("from-config", { source: "config", secret_set: false }),
  ];
  restore = mock.install();
  vi.spyOn(console, "warn").mockImplementation(() => {});
});

afterEach(() => {
  restore();
});

function fill(label: string, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
}

/** Routes are added in the "New route" dialog opened from the header. */
async function openCreate() {
  await screen.findByTestId("webhook-deploys");
  fireEvent.click(screen.getByRole("button", { name: "New route" }));
  await screen.findByRole("dialog", { name: "New route" });
}

describe("webhooks page", () => {
  it("shows the signing secret once and not after navigating back", async () => {
    renderApp("/webhooks");
    await openCreate();
    fill("Name", "Builds");
    fill("Events", "push, pull_request");
    fill("Prompt", "Summarise the build.");
    fireEvent.change(screen.getByLabelText("Signature check"), { target: { value: "github" } });
    fireEvent.click(screen.getByRole("button", { name: "Add route" }));

    const created = within(await screen.findByRole("region", { name: "New webhook route" }));
    const secret = created.getByTestId("webhook-secret").textContent ?? "";
    expect(secret.length).toBeGreaterThan(10);
    expect(created.getByText("http://127.0.0.1:8644/webhooks/builds")).toBeTruthy();
    expect(created.getByText(/You will not see it again/)).toBeTruthy();
    expect(created.getByRole("button", { name: "Copy the signing secret" })).toBeTruthy();
    expect(mock.callsTo("POST", "/api/webhooks")[0]?.body).toEqual({
      name: "builds",
      auth: "github",
      deliver_only: false,
      events: ["push", "pull_request"],
      prompt: "Summarise the build.",
    });
    await screen.findByTestId("webhook-builds");

    // Away and back (with the dialog still open, so the nav sits behind the modal): the list is
    // fetched again and carries no secret; the page shows none.
    const nav = within(screen.getByRole("navigation", { name: "Dashboard", hidden: true }));
    fireEvent.click(nav.getByRole("link", { name: "Sessions", hidden: true }));
    await waitFor(() => expect(screen.queryByTestId("webhook-builds")).toBeNull());
    fireEvent.click(nav.getByRole("link", { name: "Webhooks", hidden: true }));
    await screen.findByTestId("webhook-builds");
    expect(screen.queryByRole("region", { name: "New webhook route" })).toBeNull();
    expect(document.body.textContent).not.toContain(secret);
    for (const call of mock.callsTo("GET", "/api/webhooks")) expect(call.body).toBeUndefined();
    expect(JSON.stringify(mock.state.webhooks)).not.toContain(secret);
  });

  it("lets the person dismiss the secret after copying it", async () => {
    renderPage(<WebhooksPage />, "/webhooks");
    await openCreate();
    fill("Name", "alerts");
    fireEvent.click(screen.getByRole("button", { name: "Add route" }));
    const created = within(await screen.findByRole("region", { name: "New webhook route" }));
    fireEvent.click(created.getByRole("button", { name: "I have copied it" }));
    await waitFor(() =>
      expect(screen.queryByRole("region", { name: "New webhook route" })).toBeNull(),
    );
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("keeps New route off while the receiver is off, and offers a restart after enabling", async () => {
    mock.state.webhookReceiver = false;
    renderPage(<WebhooksPage />, "/webhooks");
    await screen.findByTestId("webhook-deploys");
    expect((screen.getByRole("button", { name: "New route" }) as HTMLButtonElement).disabled).toBe(
      true,
    );
    fireEvent.click(screen.getByRole("button", { name: "Enable webhooks" }));
    await waitFor(() =>
      expect(mock.callsTo("PUT", "/api/webhooks/receiver")[0]?.body).toEqual({ enabled: true }),
    );
    // The receiver starts with the daemon: the page offers the restart right there.
    expect(await screen.findByRole("button", { name: "Restart daemon" })).toBeTruthy();
    await waitFor(() =>
      expect(
        (screen.getByRole("button", { name: "New route" }) as HTMLButtonElement).disabled,
      ).toBe(false),
    );
    expect(screen.getByRole("switch", { name: "Webhook receiver on" })).toBeTruthy();
  });

  it("refuses a bad name and a direct delivery without a target before sending", async () => {
    renderPage(<WebhooksPage />, "/webhooks");
    await openCreate();
    fill("Name", "no spaces please");
    fireEvent.click(screen.getByRole("button", { name: "Add route" }));
    expect((await screen.findByRole("alert")).textContent).toContain("lower-case");
    fill("Name", "ok-name");
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: "Add route" }));
    await waitFor(() =>
      expect(screen.getByRole("alert").textContent).toContain("needs a delivery target"),
    );
    expect(mock.callsTo("POST", "/api/webhooks")).toHaveLength(0);
  });

  it("shows the server's refusal for a duplicate name", async () => {
    renderPage(<WebhooksPage />, "/webhooks");
    await openCreate();
    fill("Name", "deploys");
    fireEvent.click(screen.getByRole("button", { name: "Add route" }));
    expect((await screen.findByRole("alert")).textContent).toContain("already exists");
  });

  it("switches routes and the receiver, deletes after confirming, leaves config routes alone", async () => {
    renderPage(<WebhooksPage />, "/webhooks");
    const deploys = within(await screen.findByTestId("webhook-deploys"));
    expect(deploys.getByText(/Events: push/)).toBeTruthy();
    expect(deploys.getByText(/Delivery: telegram:42/)).toBeTruthy();
    fireEvent.click(deploys.getByRole("switch", { name: "Enable deploys" }));
    await waitFor(() =>
      expect(mock.callsTo("PUT", "/api/webhooks/deploys/enabled")[0]?.body).toEqual({
        enabled: false,
      }),
    );

    fireEvent.click(screen.getByRole("switch", { name: "Webhook receiver on" }));
    await waitFor(() =>
      expect(mock.callsTo("PUT", "/api/webhooks/receiver")[0]?.body).toEqual({ enabled: false }),
    );

    const config = within(screen.getByTestId("webhook-from-config"));
    expect(
      (config.getByRole("switch", { name: "Enable from-config" }) as HTMLButtonElement).disabled,
    ).toBe(true);
    expect(config.queryByRole("button", { name: "Delete" })).toBeNull();
    expect(config.getByText("no secret")).toBeTruthy();

    fireEvent.click(deploys.getByRole("button", { name: "Delete" }));
    const dialog = within(await screen.findByRole("dialog"));
    expect(mock.callsTo("DELETE", "/api/webhooks/deploys")).toHaveLength(0);
    fireEvent.click(dialog.getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(screen.queryByTestId("webhook-deploys")).toBeNull());
  });

  it("splits event lists", () => {
    expect(parseEvents(" push,\nissues , ,tag ")).toEqual(["push", "issues", "tag"]);
  });
});
