"use client";

import * as React from "react";
import { apiJson } from "../../src/lib/api";
import { useRuleMindStore } from "../../src/lib/store";
import { THEMES } from "../../src/v3/theme";

type ScheduleRecord = {
  id: string;
  policy_id: string;
  cron_expression: string;
  is_active: boolean;
  last_run_at?: string | null;
  payload_source?: Record<string, unknown>;
  config?: Record<string, unknown>;
};

export default function SchedulesPage() {
  const { apiBaseUrl, apiKey, themeMode } = useRuleMindStore();
  const theme = THEMES[themeMode];
  const [schedules, setSchedules] = React.useState<ScheduleRecord[]>([]);
  const [error, setError] = React.useState<string | null>(null);

  const load = React.useCallback(async () => {
    try {
      const response = await apiJson<ScheduleRecord[]>(apiBaseUrl, "/api/v1/schedules", {}, apiKey);
      setSchedules(response);
      setError(null);
    } catch (fetchError) {
      setError(fetchError instanceof Error ? fetchError.message : "Unable to load schedules.");
    }
  }, [apiBaseUrl, apiKey]);

  React.useEffect(() => {
    void load();
  }, [load]);

  const runNow = React.useCallback(
    async (scheduleId: string) => {
      try {
        await apiJson(apiBaseUrl, "/api/v1/schedules/" + scheduleId + "/run-now", { method: "POST" }, apiKey);
        await load();
      } catch (runError) {
        setError(runError instanceof Error ? runError.message : "Unable to run schedule.");
      }
    },
    [apiBaseUrl, apiKey, load]
  );

  return (
    <div style={{ padding: 20, display: "grid", gap: 16 }}>
      <div style={{ display: "grid", gap: 4 }}>
        <h1 style={{ margin: 0, fontSize: 24, fontWeight: 800 }}>Scheduled Jobs</h1>
        <div style={{ fontSize: 12, color: theme.muted }}>Cron-driven batch execution across tenant policies.</div>
      </div>
      {error ? <div style={{ padding: 12, borderRadius: 12, background: theme.dangerBg, color: theme.danger }}>{error}</div> : null}
      <div style={{ display: "grid", gap: 12 }}>
        {schedules.length === 0 ? (
          <div style={{ background: theme.card, border: "1px solid " + theme.border, borderRadius: 14, padding: 16, color: theme.dim, fontSize: 12 }}>
            No schedules configured yet.
          </div>
        ) : null}
        {schedules.map((schedule) => (
          <div key={schedule.id} style={{ background: theme.card, border: "1px solid " + theme.border, borderRadius: 14, padding: 16, display: "grid", gap: 10 }}>
            <div style={{ display: "flex", justifyContent: "space-between", gap: 12 }}>
              <div>
                <div style={{ fontSize: 14, fontWeight: 800 }}>{schedule.policy_id}</div>
                <div style={{ fontSize: 12, color: theme.muted }}>{schedule.cron_expression}</div>
              </div>
              <div style={{ fontSize: 11, color: schedule.is_active ? theme.success : theme.dim, fontWeight: 800 }}>
                {schedule.is_active ? "ACTIVE" : "PAUSED"}
              </div>
            </div>
            <div style={{ fontSize: 12, color: theme.muted }}>
              Last run: {schedule.last_run_at ?? "never"} · Source: {String(schedule.payload_source?.type ?? "static_json")}
            </div>
            <pre style={{ margin: 0, padding: 12, borderRadius: 12, background: theme.editor, color: theme.codeText, fontSize: 11, fontFamily: "var(--font-mono)", whiteSpace: "pre-wrap" }}>
              {JSON.stringify(schedule.config ?? {}, null, 2)}
            </pre>
            <div>
              <button type="button" onClick={() => void runNow(schedule.id)} style={{ border: "1px solid " + theme.border, background: theme.accentBg, color: theme.accent, borderRadius: 10, padding: "10px 14px", fontSize: 12, fontWeight: 700, cursor: "pointer" }}>
                Run now
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
