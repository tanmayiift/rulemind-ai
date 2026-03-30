"use client";

import { create } from "zustand";
import { persist } from "zustand/middleware";

export type ThemeMode = "light" | "dark";
export type EnvironmentName = "dev" | "uat" | "prod";

interface RuleMindUiState {
  apiBaseUrl: string;
  apiKey: string;
  environment: EnvironmentName;
  themeMode: ThemeMode;
  sidebarOpen: boolean;
  activeConnectorFilter: string;
  isMobile: boolean;
  mobileMenuOpen: boolean;
  setApiBaseUrl: (value: string) => void;
  setApiKey: (value: string) => void;
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
      apiBaseUrl: process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8080",
      apiKey: process.env.NEXT_PUBLIC_RULEMIND_DEV_API_KEY ?? "",
      environment: "dev",
      themeMode: "light",
      sidebarOpen: true,
      activeConnectorFilter: "all",
      isMobile: false,
      mobileMenuOpen: false,
      setApiBaseUrl: (value) => set({ apiBaseUrl: value }),
      setApiKey: (value) => set({ apiKey: value }),
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
      partialize: (state) => ({
        apiBaseUrl: state.apiBaseUrl,
        apiKey: state.apiKey,
        environment: state.environment,
        themeMode: state.themeMode,
        sidebarOpen: state.sidebarOpen,
        activeConnectorFilter: state.activeConnectorFilter,
      }),
    }
  )
);

export const useWorkbenchStore = useRuleMindStore;
