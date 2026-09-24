import { describe, expect, it } from "vitest";

import { translate, translatePlural } from "../src/i18n";

describe("i18n", () => {
  it("renders English by default and fills placeholders", () => {
    expect(translate("en", "signin.signedInAs", { email: "a@b.co" })).toBe("You're currently signed in as a@b.co.");
  });

  it("falls back to English key by key, and from a regional locale to its language", () => {
    const catalogs = { en: {}, de: { "signin.title": "Bei RuleMind anmelden" } };
    expect(translate("de-AT", "signin.title", undefined, catalogs)).toBe("Bei RuleMind anmelden");
    expect(translate("de-AT", "signin.submit", undefined, catalogs)).toBe("Sign in");
    expect(translate("xx", "signin.submit", undefined, catalogs)).toBe("Sign in");
  });

  it("leaves a placeholder it was not given visible rather than blank", () => {
    expect(translate("en", "signup.devKey")).toBe("Development API key for {company}");
  });

  it("chooses the plural form by the locale's rules", () => {
    expect(translatePlural("en", "signup.passwordShort", 1)).toBe("1 more character.");
    expect(translatePlural("en", "signup.passwordShort", 7)).toBe("7 more characters.");
    expect(translatePlural("en", "signup.passwordShort", 0)).toBe("0 more characters.");
  });
});
