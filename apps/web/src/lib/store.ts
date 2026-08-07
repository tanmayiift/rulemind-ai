"use client";

import { create } from "zustand";
import { persist } from "zustand/middleware";

import { setBearerToken } from "./api";

export type ThemeMode = "light" | "dark";
export type EnvironmentName = "dev" | "uat" | "prod";

export interface SessionMember {
  id: string;
  email: string;
  name: string;
  role: string;
}

interface RuleMindUiState {
  // True once zustand/persist has rehydrated from localStorage. Data fetches gate on this so they
  // never fire with the pre-hydration (empty) apiKey — which otherwise caused a burst of 401s on
  // every page before the persisted key loaded. Not persisted (runtime-only).
  hydrated: boolean;
  apiBaseUrl: string;
  apiKey: string;
  sessionToken: string;
  member: SessionMember | null;
  environment: EnvironmentName;
  themeMode: ThemeMode;
  sidebarOpen: boolean;
  activeConnectorFilter: string;
  isMobile: boolean;
  mobileMenuOpen: boolean;
  setApiBaseUrl: (value: string) => void;
  setApiKey: (value: string) => void;
  setSession: (token: string, member: SessionMember | null) => void;
  clearSession: () => void;
  setEnvironment: (value: EnvironmentName) => void;
  setThemeMode: (value: ThemeMode) => void;
  toggleThemeMode: () => void;
  setSidebarOpen: (value: boolean) => void;
  toggleSidebar: () => void;
  setActiveConnectorFilter: (value: string) => void;
  setIsMobile: (value: boolean) => void;
  setMobileMenuOpen: (value: boolean) => void;
}

export const useRuleMindStore = create<RuleMindUiState>()(
  persist(
    (set, get) => ({
      hydrated: false,
      apiBaseUrl: process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8080",
      apiKey: process.env.NEXT_PUBLIC_RULEMIND_DEV_API_KEY ?? "",
      sessionToken: "",
      member: null,
      environment: "dev",
      themeMode: "light",
      sidebarOpen: true,
      activeConnectorFilter: "all",
      isMobile: false,
      mobileMenuOpen: false,
      setApiBaseUrl: (value) => set({ apiBaseUrl: value }),
      setApiKey: (value) => set({ apiKey: value }),
      setSession: (token, member) => {
        setBearerToken(token);
        set({ sessionToken: token, member });
      },
      clearSession: () => {
        setBearerToken("");
        set({ sessionToken: "", member: null });
      },
      setEnvironment: (value) => set({ environment: value }),
      setThemeMode: (value) => set({ themeMode: value }),
      toggleThemeMode: () => set({ themeMode: get().themeMode === "light" ? "dark" : "light" }),
      setSidebarOpen: (value) => set({ sidebarOpen: value }),
      toggleSidebar: () => set({ sidebarOpen: !get().sidebarOpen }),
      setActiveConnectorFilter: (value) => set({ activeConnectorFilter: value }),
      setIsMobile: (value) => set({ isMobile: value }),
      setMobileMenuOpen: (value) => set({ mobileMenuOpen: value }),
    }),
    {
      name: "rulemind-v3-ui",
      // Flip `hydrated` once localStorage has been read, so gated fetches can run with the real key.
      // Fires even when there is no persisted state (first visit), so the app never hangs unhydrated.
      // localStorage is synchronous, so this callback can run DURING create() — before
      // `useRuleMindStore` is assigned (temporal dead zone). Defer the setState to a microtask so the
      // store reference is always defined by the time we flip the flag.
      onRehydrateStorage: () => () => {
        setTimeout(() => useRuleMindStore.setState({ hydrated: true }), 0);
      },
      partialize: (state) => ({
        apiBaseUrl: state.apiBaseUrl,
        apiKey: state.apiKey,
        // NOTE: sessionToken is deliberately NOT persisted — the member session lives in an
        // httpOnly cookie the browser sends automatically, so the bearer token is never written
        // to localStorage (removes the XSS-exfiltration surface). `member` is non-secret UI state.
        member: state.member,
        environment: state.environment,
        themeMode: state.themeMode,
        sidebarOpen: state.sidebarOpen,
        activeConnectorFilter: state.activeConnectorFilter,
      }),
    }
  )
);

export const useWorkbenchStore = useRuleMindStore;
