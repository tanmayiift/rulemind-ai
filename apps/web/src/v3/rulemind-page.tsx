"use client";


import * as React from "react";
import {
Workflow
} from "lucide-react";



import {
PageId,useTheme,
useBootstrapData,InfoBanner,
EmptyState
} from "./kit";
import { DashboardPage } from "./pages/DashboardPage";
import { ConnectorsPage } from "./pages/ConnectorsPage";
import { VariablesPage } from "./pages/VariablesPage";
import { RulesPage } from "./pages/RulesPage";
import { ScorecardsPage } from "./pages/ScorecardsPage";
import { PoliciesPage } from "./pages/PoliciesPage";
import { TestingPage } from "./pages/TestingPage";
import { AuditPage } from "./pages/AuditPage";
import { DeployPage } from "./pages/DeployPage";
import { ExportsPage } from "./pages/ExportsPage";
import { SettingsPage } from "./pages/SettingsPage";

export function RuleMindPage(props: { page: PageId }) {
  const theme = useTheme();
  const { data, error, loading, refresh, needsSetup } = useBootstrapData();
  const [notice, setNotice] = React.useState<string | null>(null);

  React.useEffect(() => {
    if (!notice) {
      return undefined;
    }
    const handle = window.setTimeout(() => setNotice(null), 3000);
    return () => window.clearTimeout(handle);
  }, [notice]);

  if (needsSetup) {
    return (
      <div style={{ padding: 24 }}>
        <EmptyState
          icon={<Workflow size={28} />}
          title="Connect RuleMind to get started"
          description="No API key is configured yet. Sign in, or add a workspace API key in Settings, to load your connectors, variables, rules, and policies."
        />
      </div>
    );
  }

  if (loading) {
    return (
      <div style={{ padding: 24 }}>
        <EmptyState icon={<Workflow size={28} />} title="Loading RuleMind" description="Fetching connectors, variables, rules, scorecards, policies, and settings." />
      </div>
    );
  }

  if (error || !data) {
    return (
      <div style={{ padding: 24 }}>
        <InfoBanner message={error ?? "Unable to load RuleMind."} toneKey="danger" />
      </div>
    );
  }

  return (
    // The per-page marker is deliberately on the SUCCESS branch only: the loading and error
    // branches above return early, so its presence means this page resolved its data, not
    // merely that the shell rendered. e2e used to assert `body` contained /rule/i, which the
    // sidebar satisfies on every route -- including the error banner -- so all seven
    // "page loads" tests passed without a page ever loading.
    <div style={{ background: theme.bg, minHeight: "100%" }} data-testid={"page-" + props.page}>
      {notice ? (
        <div style={{ padding: "16px 20px 0" }} data-testid="app-notice">
          <InfoBanner message={notice} toneKey="success" />
        </div>
      ) : null}
      {props.page === "dashboard" ? <DashboardPage data={data} /> : null}
      {props.page === "connectors" ? <ConnectorsPage data={data} refresh={refresh} onNotify={setNotice} /> : null}
      {props.page === "variables" ? <VariablesPage data={data} refresh={refresh} onNotify={setNotice} /> : null}
      {props.page === "rules" ? <RulesPage data={data} refresh={refresh} onNotify={setNotice} /> : null}
      {props.page === "scorecards" ? <ScorecardsPage data={data} refresh={refresh} onNotify={setNotice} /> : null}
      {props.page === "policies" ? <PoliciesPage data={data} refresh={refresh} onNotify={setNotice} /> : null}
      {props.page === "testing" ? <TestingPage data={data} onNotify={setNotice} /> : null}
      {props.page === "deploy" ? <DeployPage data={data} refresh={refresh} onNotify={setNotice} /> : null}
      {props.page === "audit" ? <AuditPage onNotify={setNotice} /> : null}
      {props.page === "exports" ? <ExportsPage data={data} refresh={refresh} onNotify={setNotice} /> : null}
      {props.page === "settings" ? <SettingsPage data={data} refresh={refresh} onNotify={setNotice} /> : null}
    </div>
  );
}

