import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createMockApi, type MockApi } from "../testing/mockApi.ts";
import { renderPage } from "../testing/render.tsx";
import { ageText, PairingPage } from "./PairingPage.tsx";

let mock: MockApi;
let restore: () => void;

beforeEach(() => {
  mock = createMockApi();
  mock.state.pairing = {
    pending: [
      {
        platform: "telegram",
        request_id: "r1",
        user_id: "101",
        user_name: "Asha",
        age_seconds: 120,
      },
      { platform: "signal", request_id: "r2", user_id: "202", user_name: null, age_seconds: 5 },
    ],
    approved: [
      { platform: "telegram", user_id: "303", user_name: "Ravi", approved_at: 1_760_000_000 },
    ],
  };
  mock.state.pairingCodes = {
    "ABCD-1234": { platform: "telegram", user_id: "404", user_name: "Meera" },
  };
  restore = mock.install();
  vi.spyOn(console, "warn").mockImplementation(() => {});
});

afterEach(() => {
  restore();
});

describe("pairing page", () => {
  it("approves a pending request by its id", async () => {
    renderPage(<PairingPage />, "/pairing");
    const row = within(await screen.findByTestId("pending-r1"));
    expect(row.getByText(/2 min ago/)).toBeTruthy();
    fireEvent.click(row.getByRole("button", { name: "Approve" }));
    await waitFor(() =>
      expect(mock.callsTo("POST", "/api/pairing/approve")[0]?.body).toEqual({
        platform: "telegram",
        request_id: "r1",
      }),
    );
    await screen.findByTestId("approved-telegram-101");
    expect(screen.queryByTestId("pending-r1")).toBeNull();
  });

  it("approves by the code the person received, and explains a wrong one", async () => {
    renderPage(<PairingPage />, "/pairing");
    await screen.findByTestId("pending-r1");
    const form = within(screen.getByText("Approve by code").closest("section") as HTMLElement);
    fireEvent.change(form.getByLabelText("Platform"), { target: { value: "telegram" } });
    fireEvent.change(form.getByLabelText("Pairing code"), { target: { value: "WRONG" } });
    fireEvent.click(form.getByRole("button", { name: "Approve" }));
    expect((await form.findByRole("alert")).textContent).toContain("unknown or has expired");

    fireEvent.change(form.getByLabelText("Pairing code"), { target: { value: "LOCKED" } });
    fireEvent.click(form.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(form.getByRole("alert").textContent).toContain("Too many"));

    fireEvent.change(form.getByLabelText("Pairing code"), { target: { value: " ABCD-1234 " } });
    fireEvent.click(form.getByRole("button", { name: "Approve" }));
    await screen.findByTestId("approved-telegram-404");
    expect(mock.callsTo("POST", "/api/pairing/approve").at(-1)?.body).toEqual({
      platform: "telegram",
      code: "ABCD-1234",
    });
    expect((form.getByLabelText("Pairing code") as HTMLInputElement).value).toBe("");
  });

  it("revokes an approved user after confirming", async () => {
    renderPage(<PairingPage />, "/pairing");
    const row = within(await screen.findByTestId("approved-telegram-303"));
    fireEvent.click(row.getByRole("button", { name: "Revoke" }));
    const dialog = within(await screen.findByRole("dialog"));
    expect(mock.callsTo("POST", "/api/pairing/revoke")).toHaveLength(0);
    fireEvent.click(dialog.getByRole("button", { name: "Revoke" }));
    await waitFor(() => expect(screen.queryByTestId("approved-telegram-303")).toBeNull());
    expect(mock.callsTo("POST", "/api/pairing/revoke")[0]?.body).toEqual({
      platform: "telegram",
      user_id: "303",
    });
  });

  it("clears pending requests after confirming", async () => {
    renderPage(<PairingPage />, "/pairing");
    await screen.findByTestId("pending-r2");
    fireEvent.click(screen.getByRole("button", { name: "Clear pending" }));
    const dialog = within(await screen.findByRole("dialog"));
    fireEvent.click(dialog.getByRole("button", { name: "Clear" }));
    await waitFor(() => expect(screen.queryByTestId("pending-r2")).toBeNull());
    expect(mock.callsTo("POST", "/api/pairing/clear-pending")).toHaveLength(1);
    // Nobody left: the button stays live and says so instead of clearing again.
    fireEvent.click(screen.getByRole("button", { name: "Clear pending" }));
    expect(await screen.findByText("There are no pending requests to clear.")).toBeTruthy();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(mock.callsTo("POST", "/api/pairing/clear-pending")).toHaveLength(1);
  });

  it("words ages plainly", () => {
    expect(ageText(30)).toBe("under a minute ago");
    expect(ageText(600)).toBe("10 min ago");
    expect(ageText(7200)).toBe("2 h ago");
  });
});
