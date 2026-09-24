/**
 * Internationalisation scaffold (plan 6.8). There was none -- every string was a literal in JSX.
 *
 * Adding a locale is: a catalog file typed `Messages` (partial is fine -- missing keys fall back to
 * English one by one), and an entry in CATALOGS. `t()` is typed on the English keys, so a key that
 * does not exist is a compile error, and `{name}` placeholders are filled from `vars`.
 *
 * Sign-in and signup render through it today; the rest of the app converts screen by screen.
 */
import * as React from "react";

import { en, type MessageKey, type Messages } from "./en";

export type Locale = "en";
const CATALOGS: Record<Locale, Messages> = { en };
export const DEFAULT_LOCALE: Locale = "en";

export function translate(locale: string, key: MessageKey, vars?: Record<string, string | number>,
                          catalogs: Record<string, Messages> = CATALOGS): string {
  const base = locale.split("-")[0];
  const template = catalogs[locale]?.[key] ?? catalogs[base]?.[key] ?? en[key];
  return template.replace(/\{(\w+)\}/g, (match, name: string) =>
    vars && Object.prototype.hasOwnProperty.call(vars, name) ? String(vars[name]) : match);
}

/** Keys that come in plural forms, named by their stem: "signup.passwordShort" for
 *  "signup.passwordShort.one" / ".other". */
type PluralStem = MessageKey extends infer K ? (K extends `${infer Stem}.other` ? Stem : never) : never;

/** The plural form for `count` in this locale (Intl.PluralRules), e.g. "1 more character." vs
 *  "7 more characters." -- not "character(s)". */
export function translatePlural(locale: string, stem: PluralStem, count: number,
                                vars?: Record<string, string | number>): string {
  const category = new Intl.PluralRules(locale).select(count);
  const key = (category === "one" ? stem + ".one" : stem + ".other") as MessageKey;
  return translate(locale, key, { count, ...vars });
}

export type Translate = ((key: MessageKey, vars?: Record<string, string | number>) => string) & {
  plural: (stem: PluralStem, count: number, vars?: Record<string, string | number>) => string;
};

/** The document's language (set on <html lang>), falling back to English. */
export function useT(): Translate {
  const [locale, setLocale] = React.useState<string>(DEFAULT_LOCALE);
  React.useEffect(() => {
    const lang = document.documentElement.lang;
    if (lang) setLocale(lang);
  }, []);
  return React.useMemo(() => {
    const t = ((key: MessageKey, vars?: Record<string, string | number>) => translate(locale, key, vars)) as Translate;
    t.plural = (stem, count, vars) => translatePlural(locale, stem, count, vars);
    return t;
  }, [locale]);
}

export type { MessageKey, Messages };
