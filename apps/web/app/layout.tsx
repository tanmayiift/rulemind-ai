import * as React from "react";
import type { Metadata } from "next";
import "./globals.css";
import { Providers } from "./providers";
import { AppShell } from "../src/components/app-shell";
import { themeStyleBlock } from "../src/v3/theme";

export const metadata: Metadata = {
  title: "RuleMind",
  description: "Enterprise decisioning engine for connectors, variables, rules, scorecards, policies, and operational audit.",
};

const themeScript = `
(() => {
  const themes = {
    light: ${JSON.stringify(themeStyleBlock("light"))},
    dark: ${JSON.stringify(themeStyleBlock("dark"))}
  };
  try {
    const raw = window.localStorage.getItem("rulemind-v3-ui");
    const parsed = raw ? JSON.parse(raw) : null;
    const mode = parsed?.state?.themeMode === "dark" ? "dark" : "light";
    document.documentElement.dataset.theme = mode;
    document.documentElement.setAttribute("style", themes[mode]);
  } catch (error) {
    document.documentElement.dataset.theme = "light";
    document.documentElement.setAttribute("style", themes.light);
  }
})();`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeScript }} />
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="anonymous" />
        <link
          rel="stylesheet"
          href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@400;500;700&display=swap"
        />
      </head>
      <body>
        <Providers>
          <AppShell>{children}</AppShell>
        </Providers>
      </body>
    </html>
  );
}
