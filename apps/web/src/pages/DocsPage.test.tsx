import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DOCS_LINK } from "../shell/routes.ts";
import { DocsPage } from "./DocsPage.tsx";

describe("docs page", () => {
  it("frames a pointer that opens our docs in a new tab", () => {
    render(<DocsPage />);
    const panel = screen.getByRole("region", { name: "Documentation" });
    expect(panel.textContent).toContain("Indus Agent documentation");
    const link = screen.getByRole("link", { name: "Open docs" });
    expect(link.getAttribute("href")).toBe(DOCS_LINK.href);
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("rel")).toBe("noopener noreferrer");
  });
});
