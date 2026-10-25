// Documentation (W52-docs-1; our guides in-app since W58-J2-1): the user guides of docs/user,
// bundled with the dashboard, beside the navigation of our docs site. `?page=<slug>` picks the
// guide (Home without one) and `#anchor` a heading in it, so a guide can be linked and reloaded.
// The full docs site stays one click away (DOCS_LINK). There is no API reference page: the
// backend keeps FastAPI's OpenAPI pages off.

import { useQuery } from "@tanstack/react-query";
import { BookOpen, ExternalLink } from "lucide-react";
import { useEffect, useMemo, useRef } from "react";
import { useLocation, useNavigate, useSearchParams } from "react-router";
import { DOCS_LINK } from "../shell/routes.ts";
import { Card, Loading, QueryState } from "../ui/controls.tsx";
import { cx } from "../ui/cx.ts";
import { DocArticle, type DocLinkTarget } from "./docs/DocArticle.tsx";
import { docsNav, HOME_SLUG, loadDoc } from "./docs/library.ts";

function DocsNav({ active, onPick }: { active: string; onPick: (slug: string) => void }) {
  const sections = useMemo(() => docsNav(), []);
  return (
    <nav
      aria-label="Guides"
      className="flex shrink-0 flex-col border border-line bg-surface lg:sticky lg:top-4 lg:w-[230px] lg:self-start"
    >
      <div className="flex h-[31px] items-center gap-2 border-b border-line px-3">
        <BookOpen aria-hidden="true" className="size-3 text-ink-soft" />
        <span className="type-label text-[0.6875rem] tracking-[0.12em] text-ink-muted">Guides</span>
      </div>
      <div className="max-h-[40vh] overflow-y-auto p-2 lg:max-h-[calc(100vh-220px)]">
        {sections.map((section) => (
          <div key={section.title || "home"} className="mb-2 last:mb-0">
            {section.title ? (
              <p className="type-label m-0 px-2 pt-2 pb-1 text-[0.6875rem] text-ink-soft">
                {section.title}
              </p>
            ) : null}
            <ul className="m-0 flex list-none flex-col gap-px p-0">
              {section.pages.map((page) => {
                const on = page.slug === active;
                return (
                  <li key={page.slug}>
                    <button
                      type="button"
                      aria-current={on ? "page" : undefined}
                      onClick={() => onPick(page.slug)}
                      className={cx(
                        "type-data flex min-h-[23px] w-full items-center px-2 py-0.5 text-left text-[0.75rem] transition-colors",
                        on ? "bg-selected text-ink" : "text-ink-muted hover:bg-hover",
                      )}
                    >
                      {page.title}
                    </button>
                  </li>
                );
              })}
            </ul>
          </div>
        ))}
      </div>
    </nav>
  );
}

export function DocsPage() {
  const [params] = useSearchParams();
  const location = useLocation();
  const navigate = useNavigate();
  const slug = (params.get("page") ?? "").trim() || HOME_SLUG;
  const doc = useQuery({
    queryKey: ["docs", slug],
    queryFn: () => loadDoc(slug),
    staleTime: Number.POSITIVE_INFINITY,
  });
  const top = useRef<HTMLDivElement>(null);

  // Other query values (the profile) ride along; the anchor is the URL's hash.
  const open = (target: DocLinkTarget) => {
    const next = new URLSearchParams(params);
    if (target.slug === HOME_SLUG) next.delete("page");
    else next.set("page", target.slug);
    const search = next.toString();
    navigate({
      search: search ? `?${search}` : "",
      hash: target.anchor ? `#${target.anchor}` : "",
    });
  };

  // Once the guide is on screen, bring its anchor (or its top) into view.
  const hash = location.hash;
  useEffect(() => {
    if (doc.data == null) return;
    const anchor = decodeURIComponent(hash.slice(1));
    const target = anchor ? document.getElementById(anchor) : top.current;
    target?.scrollIntoView?.({ block: "start" });
  }, [doc.data, hash]);

  return (
    <div className="flex flex-col gap-4 px-4 py-[1.4375rem] lg:flex-row lg:items-start lg:px-[1.4375rem]">
      <DocsNav active={slug} onPick={(next) => open({ slug: next, anchor: "" })} />
      <Card padded={false} className="min-w-0 flex-1">
        <section aria-label="Documentation" className="px-5 py-6 sm:px-8">
          <div ref={top} className="mb-4 flex flex-wrap items-center justify-end gap-2">
            <a
              href={DOCS_LINK.href}
              target="_blank"
              rel="noopener noreferrer"
              className="type-button inline-flex h-[1.75rem] items-center gap-2 border border-line px-2.5 text-ink-muted uppercase hover:bg-hover [&_svg]:size-3.5"
            >
              <ExternalLink aria-hidden="true" />
              Open docs
            </a>
          </div>
          {doc.isPending ? (
            <div className="flex justify-center py-12">
              <Loading />
            </div>
          ) : (
            <QueryState loading={false} error={doc.error} onRetry={() => void doc.refetch()}>
              {doc.data == null ? (
                <p className="m-0 py-8 text-center text-[0.875rem] text-ink-muted">
                  No guide is called “{slug}”.{" "}
                  <button
                    type="button"
                    className="text-ink underline underline-offset-2"
                    onClick={() => open({ slug: HOME_SLUG, anchor: "" })}
                  >
                    Go to the docs home
                  </button>
                </p>
              ) : (
                <DocArticle slug={slug} source={doc.data} onNavigate={open} />
              )}
            </QueryState>
          )}
        </section>
      </Card>
    </div>
  );
}
