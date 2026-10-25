// Documentation (W52-docs-1): an in-app page with the framed panel the docs will live in. Until
// our docs site exists (P25-20) the panel holds a pointer that opens the docs in a new tab; once
// it does, the panel becomes an iframe of `DOCS_LINK.href` (needs a `frame-src` for that origin).
// W57-H1-1: the panel takes the theme's surface and ink tokens (it was a fixed white sheet with
// navy text, so the dark themes showed a bright unthemed block).

import { BookOpen, ExternalLink } from "lucide-react";
import { DOCS_LINK } from "../shell/routes.ts";

export function DocsPage() {
  return (
    <div className="flex min-h-0 flex-1 flex-col px-[1.4375rem] py-[1.875rem]">
      <section
        aria-label="Documentation"
        className="flex min-h-[24rem] flex-1 flex-col items-center justify-center gap-3 rounded-[2px] border border-line bg-surface px-6 text-center"
      >
        <BookOpen aria-hidden="true" className="size-8 text-ink-muted" />
        <h2 className="m-0 text-[1.125rem] font-semibold text-ink">Indus Agent documentation</h2>
        <p className="m-0 max-w-md text-[0.875rem] text-ink-muted">
          The guides and reference pages open in a new browser tab.
        </p>
        <a
          href={DOCS_LINK.href}
          target="_blank"
          rel="noopener noreferrer"
          className="type-button mt-2 inline-flex h-[1.875rem] items-center gap-2 border border-ink px-3 text-ink uppercase hover:bg-hover [&_svg]:size-3.5"
        >
          <ExternalLink aria-hidden="true" />
          Open docs
        </a>
      </section>
    </div>
  );
}
