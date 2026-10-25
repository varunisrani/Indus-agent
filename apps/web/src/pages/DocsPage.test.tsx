import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { DOCS_LINK } from "../shell/routes.ts";
import { createMockApi, type MockApi } from "../testing/mockApi.ts";
import { currentLocation, renderPage } from "../testing/render.tsx";
import { DocsPage } from "./DocsPage.tsx";

let mock: MockApi;
let restore: () => void;

beforeEach(() => {
  mock = createMockApi();
  restore = mock.install();
});

afterEach(() => {
  restore();
});

describe("docs page", () => {
  it("renders our docs home with the site's navigation and a link out", async () => {
    renderPage(<DocsPage />, "/docs");
    const panel = screen.getByRole("region", { name: "Documentation" });
    expect(
      await within(panel).findByRole("heading", { level: 1, name: "Indus Agent" }),
    ).toBeTruthy();
    const nav = within(screen.getByRole("navigation", { name: "Guides" }));
    expect(nav.getByRole("button", { name: "Home" }).getAttribute("aria-current")).toBe("page");
    expect(nav.getByText("Getting started")).toBeTruthy();
    expect(nav.getByRole("button", { name: "Quickstart" })).toBeTruthy();
    const out = screen.getByRole("link", { name: "Open docs" });
    expect(out.getAttribute("href")).toBe(DOCS_LINK.href);
    expect(out.getAttribute("target")).toBe("_blank");
    expect(out.getAttribute("rel")).toBe("noopener noreferrer");
    // Tables render as tables, never as raw HTML.
    expect(within(panel).getAllByRole("table").length).toBeGreaterThan(0);
  });

  it("opens a guide from the navigation and from a link inside a guide", async () => {
    renderPage(<DocsPage />, "/docs");
    const panel = screen.getByRole("region", { name: "Documentation" });
    await within(panel).findByRole("heading", { level: 1, name: "Indus Agent" });
    fireEvent.click(within(panel).getAllByRole("link", { name: "Install" })[0] as HTMLElement);
    await waitFor(() => expect(currentLocation()).toBe("/docs?page=getting-started%2Finstall"));
    expect(await within(panel).findByRole("heading", { level: 1 })).toBeTruthy();
    const nav = within(screen.getByRole("navigation", { name: "Guides" }));
    expect(nav.getByRole("button", { name: "Install" }).getAttribute("aria-current")).toBe("page");
    fireEvent.click(nav.getByRole("button", { name: "Command line" }));
    await waitFor(() => expect(currentLocation()).toBe("/docs?page=reference%2Fcli"));
    expect(
      await within(panel).findByRole("heading", { level: 1, name: "Command line" }),
    ).toBeTruthy();
  });

  it("opens web addresses in a new tab", async () => {
    renderPage(<DocsPage />, "/docs?page=getting-started/install");
    const panel = screen.getByRole("region", { name: "Documentation" });
    await within(panel).findByRole("heading", { level: 1 });
    const external = within(panel)
      .getAllByRole("link")
      .filter((a) => /^https?:/.test(a.getAttribute("href") ?? ""));
    for (const link of external) {
      expect(link.getAttribute("target")).toBe("_blank");
      expect(link.getAttribute("rel")).toBe("noopener noreferrer");
    }
  });

  it("says so for a guide that does not exist", async () => {
    renderPage(<DocsPage />, "/docs?page=nope/missing");
    expect(await screen.findByText(/No guide is called/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Go to the docs home" }));
    await waitFor(() => expect(currentLocation()).toBe("/docs"));
  });

  it("draws the panel with theme tokens, never a fixed light sheet (W57-H1-1)", async () => {
    const { container } = renderPage(<DocsPage />, "/docs");
    const panel = screen.getByRole("region", { name: "Documentation" });
    await within(panel).findByRole("heading", { level: 1, name: "Indus Agent" });
    // the panel sits on a theme surface token (the card is bg-canvas), never a fixed sheet
    expect(panel.closest(".bg-canvas, .bg-surface")).not.toBeNull();
    // no fixed colours anywhere in the page: dark themes must not get a white block or navy text
    for (const el of container.querySelectorAll("*")) {
      const cls = el.getAttribute("class") ?? "";
      expect(cls).not.toMatch(/bg-white|color-scheme:light|#[0-9a-fA-F]{3,8}/);
    }
  });
});
