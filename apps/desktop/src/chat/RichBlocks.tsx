// Code highlighting (Shiki), math (KaTeX) and diagrams (Mermaid), each loaded on first use. Until
// a renderer has loaded — or when it fails — the plain text shows, so nothing is ever blank.
// Shiki runs on its JavaScript regex engine: the CSP allows no `wasm-unsafe-eval`. Mermaid runs
// with `securityLevel: "strict"`. The HTML these libraries return is their own escaped output.

import { Copy } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";
import { copyText } from "../app/clipboard.ts";
import { useT } from "../i18n/index.tsx";

export interface RichLoaders {
  highlight(code: string, lang: string): Promise<string | null>;
  math(tex: string, display: boolean): Promise<string | null>;
  diagram(id: string, source: string, dark: boolean): Promise<string | null>;
}

type ShikiHighlighter = {
  getLoadedLanguages(): string[];
  loadLanguage(...langs: string[]): Promise<void>;
  codeToHtml(code: string, options: Record<string, unknown>): string;
};

let shiki: Promise<{ highlighter: ShikiHighlighter; known: Set<string> }> | null = null;

function loadShiki() {
  shiki ??= import("shiki").then(async (module) => {
    const highlighter = (await module.createHighlighter({
      themes: ["github-light", "github-dark"],
      langs: [],
      engine: module.createJavaScriptRegexEngine(),
    })) as unknown as ShikiHighlighter;
    return { highlighter, known: new Set(Object.keys(module.bundledLanguages)) };
  });
  return shiki;
}

let katexCss: Promise<unknown> | null = null;

/** The real loaders; tests swap them with `setRichLoaders`. */
export const defaultLoaders: RichLoaders = {
  async highlight(code, lang) {
    const { highlighter, known } = await loadShiki();
    const language = known.has(lang) ? lang : "text";
    if (language !== "text" && !highlighter.getLoadedLanguages().includes(language)) {
      await highlighter.loadLanguage(language);
    }
    return highlighter.codeToHtml(code, {
      lang: language,
      themes: { light: "github-light", dark: "github-dark" },
      defaultColor: false,
    });
  },
  async math(tex, display) {
    katexCss ??= import("katex/dist/katex.min.css");
    const [{ default: katex }] = await Promise.all([import("katex"), katexCss]);
    return katex.renderToString(tex, {
      displayMode: display,
      throwOnError: false,
      trust: false,
      strict: "ignore",
    });
  },
  async diagram(id, source, dark) {
    const { default: mermaid } = await import("mermaid");
    mermaid.initialize({
      startOnLoad: false,
      securityLevel: "strict",
      theme: dark ? "dark" : "default",
    });
    const { svg } = await mermaid.render(id, source);
    return svg;
  },
};

let loaders: RichLoaders = defaultLoaders;

export function setRichLoaders(next: RichLoaders | null): void {
  loaders = next ?? defaultLoaders;
}

/** Run `make` when `key` changes; the latest answer wins, a failure leaves the plain text. */
function useRendered(key: string, make: (() => Promise<string | null>) | null): string | null {
  const [html, setHtml] = useState<string | null>(null);
  const maker = useRef(make);
  maker.current = make;
  useEffect(() => {
    const run = maker.current;
    if (run === null || key === "") return;
    let live = true;
    run().then(
      (value) => {
        if (live) setHtml(value);
      },
      () => {
        if (live) setHtml(null);
      },
    );
    return () => {
      live = false;
    };
  }, [key]);
  return html;
}

function isDark(): boolean {
  return document.documentElement.dataset.theme === "indus-dark";
}

/** The one place library HTML enters the DOM: Shiki's escaped code, KaTeX with trust off,
 * Mermaid's SVG in strict mode. */
function LibraryHtml(props: { tag: "div" | "span"; className: string; html: string }) {
  const Tag = props.tag;
  // biome-ignore lint/security/noDangerouslySetInnerHtml: output of the libraries named above
  return <Tag className={props.className} dangerouslySetInnerHTML={{ __html: props.html }} />;
}

export function CodeBlock(props: { code: string; lang: string; closed: boolean }) {
  const { code, lang, closed } = props;
  // Highlight once the fence is closed: a streaming block stays plain (no re-highlight per delta).
  const html = useRendered(
    closed ? `${lang}\u0000${code}` : "",
    closed ? () => loaders.highlight(code, lang) : null,
  );
  return (
    <div className="md-code-wrap">
      {html !== null ? (
        <LibraryHtml tag="div" className="md-code" html={html} />
      ) : (
        <pre className="md-code" data-lang={lang}>
          <code>{code}</code>
        </pre>
      )}
      {closed && <CopyCode code={code} />}
    </div>
  );
}

/** The copy button a finished code block shows on hover (W52-g). */
function CopyCode(props: { code: string }) {
  const t = useT();
  return (
    <button
      type="button"
      className="desk-icon-btn md-code-copy"
      aria-label={t("markdown.copyCode")}
      onClick={() => void copyText(props.code)}
    >
      <Copy aria-hidden="true" size={13} />
    </button>
  );
}

export function MathBlock(props: { tex: string; display: boolean; closed?: boolean }) {
  const { tex, display, closed = true } = props;
  const html = useRendered(
    closed ? `${display}\u0000${tex}` : "",
    closed ? () => loaders.math(tex, display) : null,
  );
  const className = display ? "md-math" : "md-math-inline";
  if (html !== null)
    return <LibraryHtml tag={display ? "div" : "span"} className={className} html={html} />;
  const Tag = display ? "div" : "span";
  return <Tag className={`${className} md-plain`}>{tex}</Tag>;
}

export function DiagramBlock(props: { source: string; closed: boolean }) {
  const { source, closed } = props;
  const id = `mmd-${useId().replace(/[^a-zA-Z0-9]/g, "")}`;
  const html = useRendered(
    closed ? source : "",
    closed ? () => loaders.diagram(id, source, isDark()) : null,
  );
  if (html !== null) return <LibraryHtml tag="div" className="md-diagram" html={html} />;
  return (
    <pre className="md-code" data-lang="mermaid">
      <code>{source}</code>
    </pre>
  );
}
