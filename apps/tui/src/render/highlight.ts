/**
 * Small per-line code colouring: keywords, strings, comments and numbers, by language family.
 * Not a parser: state does not cross lines, which is enough for a chat transcript.
 */
import type { Theme } from "../theme/theme.ts";
import type { Span } from "./styled.tsx";
import { type ToneName, tone } from "./tones.ts";

interface Family {
  keywords: ReadonlySet<string>;
  /** Line-comment openers. */
  comments: readonly string[];
}

const words = (list: string) => new Set(list.split(" "));

const C_LIKE = words(
  "if else for while do return break continue switch case default new delete try catch finally throw class struct enum interface extends implements public private protected static const let var function import export from as async await yield typeof instanceof void null true false this super in of type fn func go defer package mut impl trait use pub match loop where self crate",
);
const PYTHON = words(
  "def class return if elif else for while in not and or is None True False import from as with try except finally raise pass break continue lambda yield global nonlocal async await assert del match case",
);
const SHELL = words(
  "if then else elif fi for in do done while until case esac function return export local echo exit set unset source alias cd",
);
const SQL = words(
  "select from where insert into values update set delete create table drop alter join left right inner outer on group by order having limit as and or not null distinct union",
);

const FAMILIES: Record<string, Family> = {
  c: { keywords: C_LIKE, comments: ["//"] },
  py: { keywords: PYTHON, comments: ["#"] },
  sh: { keywords: SHELL, comments: ["#"] },
  sql: { keywords: SQL, comments: ["--"] },
  data: { keywords: words("true false null"), comments: ["#"] },
};

const LANGS: Record<string, keyof typeof FAMILIES> = {
  js: "c",
  jsx: "c",
  ts: "c",
  tsx: "c",
  javascript: "c",
  typescript: "c",
  java: "c",
  kotlin: "c",
  c: "c",
  cpp: "c",
  "c++": "c",
  cs: "c",
  csharp: "c",
  go: "c",
  rust: "c",
  rs: "c",
  swift: "c",
  php: "c",
  py: "py",
  python: "py",
  rb: "py",
  ruby: "py",
  sh: "sh",
  bash: "sh",
  zsh: "sh",
  shell: "sh",
  console: "sh",
  sql: "sql",
  json: "data",
  yaml: "data",
  yml: "data",
  toml: "data",
};

export function canHighlight(lang: string): boolean {
  return lang.toLowerCase() in LANGS;
}

const TOKEN =
  /("(?:[^"\\]|\\.)*"?|'(?:[^'\\]|\\.)*'?|`(?:[^`\\]|\\.)*`?)|(\b\d[\d_]*(?:\.\d+)?\b)|([A-Za-z_][\w$]*)|(\s+)|(.)/g;

export function highlightLine(line: string, lang: string, theme: Theme): Span[] {
  const family = FAMILIES[LANGS[lang.toLowerCase()] ?? ""];
  const base = tone(theme, "code");
  const span = (text: string, name?: ToneName): Span => {
    const color = name ? tone(theme, name) : base;
    return color ? { text, color } : { text };
  };
  if (!family) return [span(line)];
  const out: Span[] = [];
  for (const match of line.matchAll(TOKEN)) {
    const [whole, str, num, word] = match;
    const rest = line.slice(match.index ?? 0);
    if (!str && family.comments.some((opener) => rest.startsWith(opener))) {
      out.push(span(rest, "comment"));
      break;
    }
    if (str) out.push(span(whole, "string"));
    else if (num) out.push(span(whole, "number"));
    else if (word) {
      const key = family === FAMILIES.sql ? word.toLowerCase() : word;
      out.push(span(whole, family.keywords.has(key) ? "keyword" : undefined));
    } else out.push(span(whole));
  }
  return out;
}
