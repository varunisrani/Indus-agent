// @vitest-environment jsdom
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { fakeRest } from "../../settings/fakeRest.ts";
import { WebhooksOverlay } from "./WebhooksPage.tsx";

describe("WebhooksOverlay", () => {
  it("offers to enable the receiver, then a restart; New subscription waits for it", async () => {
    const api = fakeRest();
    let enabled = false;
    api.route("GET", "/api/webhooks", () => ({
      status: 200,
      body: { receiver_enabled: enabled, base_url: "http://h", routes: [] },
    }));
    api.route("PUT", "/api/webhooks/receiver", () => {
      enabled = true;
      return { status: 200, body: { ok: true, receiver_enabled: true, restart_needed: true } };
    });
    api.route("POST", "/api/daemon/restart", { ok: true, action: "daemon-restart" });
    render(<WebhooksOverlay api={api} profile="work" />);
    expect(await screen.findByText("Webhook receiver disabled")).toBeTruthy();
    expect(
      (screen.getByRole("button", { name: "New subscription" }) as HTMLButtonElement).disabled,
    ).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Enable webhooks" }));
    expect(await screen.findByText("Restart needed")).toBeTruthy();
    await waitFor(() =>
      expect(
        (screen.getByRole("button", { name: "New subscription" }) as HTMLButtonElement).disabled,
      ).toBe(false),
    );
    fireEvent.click(screen.getByRole("button", { name: "Restart gateway" }));
    await waitFor(() => expect(screen.queryByText("Restart needed")).toBeNull());
    expect(api.calls.find((c) => c.method === "PUT")?.body).toEqual({ enabled: true });
  });

  it("shows the signing secret once, after the create", async () => {
    const api = fakeRest();
    const secret = ["whsec", "fake", String(Date.now())].join("-");
    api.route("GET", "/api/webhooks", {
      receiver_enabled: true,
      base_url: "http://h",
      routes: [],
    });
    api.route("POST", "/api/webhooks", {
      name: "gh",
      url: "http://h/webhooks/gh",
      secret,
    });
    render(<WebhooksOverlay api={api} profile="work" />);
    fireEvent.click(await screen.findByRole("button", { name: "New subscription" }));
    fireEvent.change(await screen.findByLabelText("Name"), { target: { value: "gh" } });
    fireEvent.change(screen.getByLabelText(/^Events/), { target: { value: "push, ping" } });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));
    expect((await screen.findByTestId("webhook-secret")).textContent).toBe(secret);
    expect(api.calls.find((c) => c.method === "POST")?.body).toMatchObject({
      name: "gh",
      events: ["push", "ping"],
      auth: "auto",
    });
    fireEvent.click(screen.getByRole("button", { name: "Done" }));
    await waitFor(() => expect(screen.queryByTestId("webhook-secret")).toBeNull());
  });

  it("a config route is read-only; a subscribed route has a row menu", async () => {
    const api = fakeRest();
    const route = (name: string, source: string) => ({
      name,
      source,
      url: `http://h/webhooks/${name}`,
      auth: "github",
      events: ["push"],
      prompt: "Summarise",
      deliver: null,
      description: "",
      created_at: null,
      enabled: true,
      secret_set: true,
    });
    api.route("GET", "/api/webhooks", {
      receiver_enabled: true,
      base_url: "http://h",
      routes: [route("from-config", "config"), route("mine", "subscribed")],
    });
    render(<WebhooksOverlay api={api} profile="work" />);
    expect(await screen.findByText("Subscriptions (2)")).toBeTruthy();
    expect(screen.queryByLabelText("Actions for from-config")).toBeNull();
    expect(screen.getByLabelText("Actions for mine")).toBeTruthy();
    fireEvent.click(screen.getByText("from-config"));
    expect(await screen.findByText("config")).toBeTruthy();
    expect(screen.getByText("http://h/webhooks/from-config")).toBeTruthy();
  });
});
