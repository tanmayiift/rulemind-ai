import * as React from "react";
import type { Metadata } from "next";
import { Inter, JetBrains_Mono } from "next/font/google";
import "./globals.css";
import { Providers } from "./providers";
import { AppShell } from "../src/components/app-shell";
import { themeStyleBlock } from "../src/v3/theme";

// Self-hosted via next/font: no render-blocking Google Fonts request, no layout
// shift (size-adjust fallback), and the files are preloaded — better FCP/LCP.
const inter = Inter({
  subsets: ["latin"],
  weight: ["400", "500", "600", "700", "800", "900"],
  variable: "--font-inter",
  display: "swap",
});
const jetbrainsMono = JetBrains_Mono({
  subsets: ["latin"],
  weight: ["400", "500", "700"],
  variable: "--font-jetbrains-mono",
  display: "swap",
});

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
    <html lang="en" suppressHydrationWarning className={`${inter.variable} ${jetbrainsMono.variable}`}>
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeScript }} />
      </head>
      <body>
        <Providers>
          <AppShell>{children}</AppShell>
        </Providers>
      </body>
    </html>
  );
}
