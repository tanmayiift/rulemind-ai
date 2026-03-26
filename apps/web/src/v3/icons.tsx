"use client";

import * as React from "react";
import {
  Activity,
  BarChart3,
  Box,
  Building2,
  FileText,
  LayoutDashboard,
  Plug,
  Rocket,
  ScrollText,
  Settings as SettingsIcon,
  Shield,
  Smartphone,
  UserCheck,
  Variable,
  Download,
  FlaskConical,
  GitBranch,
  TrendingUp,
  Zap,
} from "lucide-react";

export const CONNECTOR_ICON_MAP = {
  bureau: Building2,
  bank: FileText,
  gst: BarChart3,
  device: Smartphone,
  kyc: UserCheck,
  custom: Zap,
} as const;

export const PAGE_ICON_MAP = {
  dashboard: LayoutDashboard,
  connectors: Plug,
  variables: Variable,
  rules: GitBranch,
  scorecards: TrendingUp,
  policies: Shield,
  testing: FlaskConical,
  deploy: Rocket,
  audit: ScrollText,
  exports: Download,
  settings: SettingsIcon,
  health: Activity,
  fallback: Box,
} as const;

export function ConnectorIcon(props: { connectorId: string; size?: number; color?: string }) {
  const Icon = CONNECTOR_ICON_MAP[props.connectorId as keyof typeof CONNECTOR_ICON_MAP] ?? Box;
  return <Icon size={props.size ?? 16} color={props.color} strokeWidth={1.9} />;
}

export function PageIcon(props: { name: string; size?: number; color?: string }) {
  const Icon = PAGE_ICON_MAP[props.name as keyof typeof PAGE_ICON_MAP] ?? PAGE_ICON_MAP.fallback;
  return <Icon size={props.size ?? 16} color={props.color} strokeWidth={1.9} />;
}

export function ConnectorIconMark(props: { connectorId: string; color?: string; label?: string }) {
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
      <ConnectorIcon connectorId={props.connectorId} color={props.color} />
      {props.label ? <span>{props.label}</span> : null}
    </span>
  );
}
