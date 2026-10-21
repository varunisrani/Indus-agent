// The i18n mechanism (P20-15). English only in 1.0 (D-16.51), but every visible string goes
// through `useT()` so a second catalog is a data change. Keys are typed: a missing one fails the
// typecheck. `{name}` placeholders are filled from `vars`.

import { createContext, type ReactNode, useContext } from "react";
import { en } from "./en.ts";

export type MessageKey = keyof typeof en;
export type Vars = Readonly<Record<string, string | number>>;
export type Translate = (key: MessageKey, vars?: Vars) => string;
export type Catalog = Readonly<Record<MessageKey, string>>;

export const messageKeys = Object.keys(en) as MessageKey[];

export function format(template: string, vars?: Vars): string {
  if (vars === undefined) return template;
  return template.replace(/\{(\w+)\}/g, (whole, name: string) =>
    name in vars ? String(vars[name]) : whole,
  );
}

export function makeTranslate(catalog: Catalog = en): Translate {
  return (key, vars) => format(catalog[key] ?? key, vars);
}

/** For code outside React (document title, notifications). */
export const translate: Translate = makeTranslate(en);
/** Short alias of `translate`. */
export const t: Translate = translate;

const I18nContext = createContext<Translate>(translate);

export function I18nProvider(props: { catalog?: Catalog; children: ReactNode }) {
  const t = props.catalog === undefined ? translate : makeTranslate(props.catalog);
  return <I18nContext.Provider value={t}>{props.children}</I18nContext.Provider>;
}

export function useT(): Translate {
  return useContext(I18nContext);
}
