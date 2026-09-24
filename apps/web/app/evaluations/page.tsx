"use client";

import * as React from "react";
import { LineChart, Trash2, Play, Upload, X } from "lucide-react";
import { apiJson } from "../../src/lib/api";
import { useRuleMindStore } from "../../src/lib/store";
import { Button, Card, Field, Input, Select, Textarea, Badge, EmptyState, PageHeader, SectionTitle, Stat } from "../../src/v3/ui";
import { datasetColumns, parseDatasetFile, parseRows } from "../../src/lib/csv";

type EvalSummary = { id: string; name: string; task: string; gate_status: string; model_id?: string | null; created_at?: string };
type EvalDetail = EvalSummary & {
  metrics: Record<string, any>;
  segments: Record<string, any>;
  temporal: Record<string, any>;
  gate_result: Record<string, any>;
  dataset_summary: Record<string, any>;
};
type Model = { id: string; name: string };

const fmt = (v: any, d = 4) => (v === null || v === undefined ? "—" : typeof v === "number" ? v.toFixed(d) : String(v));

export default function EvaluationsPage() {
  const { apiBaseUrl, apiKey } = useRuleMindStore();
  const [evals, setEvals] = React.useState<EvalSummary[]>([]);
  const [models, setModels] = React.useState<Model[]>([]);
  const [detail, setDetail] = React.useState<EvalDetail | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [status, setStatus] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);

  const [mode, setMode] = React.useState<"dataset" | "model">("dataset");
  const [name, setName] = React.useState("");
  const [task, setTask] = React.useState("binary");
  const [scoreCol, setScoreCol] = React.useState("score");
  const [labelCol, setLabelCol] = React.useState("label");
  const [segmentCol, setSegmentCol] = React.useState("");
  const [dateCol, setDateCol] = React.useState("");
  const [modelId, setModelId] = React.useState("");
  const [features, setFeatures] = React.useState("");
  const [rowsText, setRowsText] = React.useState('[\n  {"score": 0.92, "label": 1},\n  {"score": 0.11, "label": 0}\n]');
  // An uploaded dataset is held as rows, never written into the textarea: a 50k-row CSV as a
  // controlled textarea value is megabytes re-rendered on every keystroke.
  const [fileRows, setFileRows] = React.useState<Record<string, unknown>[] | null>(null);
  const [fileName, setFileName] = React.useState("");
  const fileInput = React.useRef<HTMLInputElement>(null);
  const columns = React.useMemo(() => (fileRows ? datasetColumns(fileRows) : []), [fileRows]);

  async function ingestFile(file: File) {
    setError(null);
    try {
      const rows = await parseDatasetFile(file);
      setFileRows(rows);
      setFileName(file.name);
    } catch (e) {
      setFileRows(null);
      setFileName("");
      setError(e instanceof Error ? e.message : "Could not read the file.");
    } finally {
      if (fileInput.current) fileInput.current.value = "";
    }
  }

  function clearFile() {
    setFileRows(null);
    setFileName("");
  }

  /** Columns the run will read, checked against the file BEFORE the request -- a typo'd label
   *  column is the user's to fix, not a server error to decode. */
  function missingColumns(): string[] {
    if (!fileRows) return [];
    const wanted = mode === "model"
      ? [labelCol, ...features.split(",").map((f) => f.trim()).filter(Boolean)]
      : [scoreCol, labelCol, segmentCol, dateCol].filter(Boolean);
    return wanted.filter((c) => !columns.includes(c));
  }

  const load = React.useCallback(async () => {
    try {
      const [ev, md] = await Promise.all([
        apiJson<EvalSummary[]>(apiBaseUrl, "/api/v1/evaluations", {}, apiKey),
        apiJson<Model[]>(apiBaseUrl, "/api/v1/models", {}, apiKey).catch(() => []),
      ]);
      setEvals(ev);
      setModels(md);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [apiBaseUrl, apiKey]);

  React.useEffect(() => {
    void load();
  }, [load]);

  async function run() {
    setBusy(true);
    setError(null);
    setStatus(null);
    try {
      const rows = fileRows ?? parseRows(rowsText);
      if (!rows.length) throw new Error("Provide at least one data row: upload a file, or paste a JSON array or CSV.");
      const missing = missingColumns();
      if (missing.length) {
        throw new Error(`Not in ${fileName}: ${missing.join(", ")}. Its columns are: ${columns.join(", ")}.`);
      }
      let result: EvalDetail;
      if (mode === "model") {
        result = await apiJson<EvalDetail>(
          apiBaseUrl,
          `/api/v1/evaluations/from-model/${encodeURIComponent(modelId)}`,
          {
            method: "POST",
            body: JSON.stringify({
              name: name || "Model evaluation",
              features: features.split(",").map((f) => f.trim()).filter(Boolean),
              label_col: labelCol,
              rows,
            }),
          },
          apiKey
        );
      } else {
        const config: Record<string, any> = { score_col: scoreCol, label_col: labelCol };
        if (segmentCol) config.segment_col = segmentCol;
        if (dateCol) {
          config.date_col = dateCol;
          config.date_freq = "month";
        }
        result = await apiJson<EvalDetail>(
          apiBaseUrl,
          "/api/v1/evaluations",
          { method: "POST", body: JSON.stringify({ name: name || "Evaluation", task, config, rows }) },
          apiKey
        );
      }
      setDetail(result);
      setStatus(`Evaluation "${result.name}" complete — gate ${result.gate_status.toUpperCase()}.`);
      void load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function open(id: string) {
    try {
      setDetail(await apiJson<EvalDetail>(apiBaseUrl, `/api/v1/evaluations/${id}`, {}, apiKey));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function remove(id: string) {
    try {
      await apiJson(apiBaseUrl, `/api/v1/evaluations/${id}`, { method: "DELETE" }, apiKey);
      if (detail?.id === id) setDetail(null);
      void load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  const m = detail?.metrics ?? {};
  const gateTone = detail?.gate_status === "pass" ? "success" : detail?.gate_status === "fail" ? "danger" : "neutral";

  return (
    <div className="space-y-6">
      <PageHeader
        title="Model Evaluation"
        subtitle="Gini / AUC / KS / PR-AUC / calibration / confusion, multi-label & uplift, temporal backtest, and a promotion-gate verdict — scored against ground-truth labels. Population drift (PSI) lives on Predictors, which has the baseline to compare against."
      />

      {error && <Card><div style={{ color: "var(--danger, #b42318)" }}>{error}</div></Card>}
      {status && <Card><div style={{ color: "var(--success, #067647)" }}>{status}</div></Card>}

      <Card>
        <SectionTitle>Run an evaluation</SectionTitle>
        <div className="grid gap-3" style={{ gridTemplateColumns: "repeat(auto-fit,minmax(180px,1fr))" }}>
          <Field label="Mode">
            <Select value={mode} onChange={(e) => setMode(e.target.value as "dataset" | "model")}>
              <option value="dataset">Scored dataset</option>
              <option value="model">From hosted model</option>
            </Select>
          </Field>
          <Field label="Name"><Input value={name} onChange={(e) => setName(e.target.value)} placeholder="Q3 PD model" /></Field>
          {mode === "dataset" ? (
            <>
              <Field label="Task">
                <Select value={task} onChange={(e) => setTask(e.target.value)}>
                  <option value="binary">Binary (PD / fraud)</option>
                  <option value="multilabel">Multi-label (propensity)</option>
                  <option value="uplift">Uplift (cross-sell)</option>
                </Select>
              </Field>
              <Field label="Score column"><Input value={scoreCol} list="eval-columns" onChange={(e) => setScoreCol(e.target.value)} /></Field>
              <Field label="Label column"><Input value={labelCol} list="eval-columns" onChange={(e) => setLabelCol(e.target.value)} /></Field>
              <Field label="Segment column (optional)"><Input value={segmentCol} list="eval-columns" onChange={(e) => setSegmentCol(e.target.value)} /></Field>
              <Field label="Date column (optional, backtest)"><Input value={dateCol} list="eval-columns" onChange={(e) => setDateCol(e.target.value)} /></Field>
            </>
          ) : (
            <>
              <Field label="Hosted model">
                <Select value={modelId} onChange={(e) => setModelId(e.target.value)}>
                  <option value="">Select a model…</option>
                  {models.map((mo) => <option key={mo.id} value={mo.id}>{mo.name}</option>)}
                </Select>
              </Field>
              <Field label="Features (comma-separated)"><Input value={features} onChange={(e) => setFeatures(e.target.value)} placeholder={columns.length ? columns.filter((c) => c !== labelCol).join(", ") : "feature_0, feature_1"} /></Field>
              <Field label="Label column"><Input value={labelCol} list="eval-columns" onChange={(e) => setLabelCol(e.target.value)} /></Field>
            </>
          )}
        </div>
        <datalist id="eval-columns">{columns.map((c) => <option key={c} value={c} />)}</datalist>
        <div className="mt-3 flex flex-wrap items-center gap-3">
          <input
            ref={fileInput}
            id="eval-dataset-file"
            type="file"
            accept=".csv,.xls,.xlsx,.json,.jsonl,.ndjson"
            className="rm-sr-only"
            onChange={(e) => { const f = e.target.files?.[0]; if (f) void ingestFile(f); }}
          />
          <Button variant="secondary" onClick={() => fileInput.current?.click()} aria-controls="eval-dataset-file">
            <Upload size={16} /> Upload dataset
          </Button>
          {fileRows ? (
            <span className="flex items-center gap-2" data-testid="eval-dataset-summary">
              <Badge tone="accent">{fileName}</Badge>
              <span>{fileRows.length.toLocaleString()} rows · {columns.length} columns</span>
              <Button variant="ghost" onClick={clearFile} aria-label={`Remove ${fileName}`}><X size={14} /></Button>
            </span>
          ) : (
            <span style={{ color: "var(--muted)" }}>.csv, .xlsx, .json or .jsonl — or paste rows below</span>
          )}
        </div>
        {fileRows ? (
          <div className="mt-2" style={{ color: "var(--muted)" }}>Columns: {columns.join(", ")}</div>
        ) : (
          <Field label="Data rows — JSON array or CSV (score/label + optional segment/date/feature columns)">
            <Textarea mono value={rowsText} onChange={(e) => setRowsText(e.target.value)} rows={8} />
          </Field>
        )}
        <div className="mt-2">
          <Button onClick={run} disabled={busy}><Play size={16} /> {busy ? "Evaluating…" : "Run evaluation"}</Button>
        </div>
      </Card>

      {detail && (
        <Card>
          <SectionTitle right={<Badge tone={gateTone as any}>Gate: {detail.gate_status.toUpperCase()}</Badge>}>
            {detail.name} · {detail.task}
          </SectionTitle>
          {detail.task === "binary" && (
            <div className="grid gap-3" style={{ gridTemplateColumns: "repeat(auto-fit,minmax(120px,1fr))" }}>
              <Stat label="Gini" value={fmt(m.gini)} tone="accent" />
              <Stat label="AUC" value={fmt(m.auc)} />
              <Stat label="KS" value={fmt(m.ks)} />
              <Stat label="PR-AUC" value={fmt(m.pr_auc)} />
              <Stat label="Brier" value={fmt(m.brier)} />
              <Stat label="ECE" value={fmt(m.ece)} />
              <Stat label="Base rate" value={fmt(m.base_rate)} />
              <Stat label="N" value={fmt(m.n, 0)} />
            </div>
          )}
          {detail.task === "multilabel" && (
            <div className="grid gap-3" style={{ gridTemplateColumns: "repeat(auto-fit,minmax(120px,1fr))" }}>
              <Stat label="Macro AUC" value={fmt(m.macro_auc)} tone="accent" />
              <Stat label="mAP" value={fmt(m.mean_average_precision)} />
              <Stat label="P@1" value={fmt(m.precision_at_1)} />
              <Stat label="P@3" value={fmt(m.precision_at_3)} />
            </div>
          )}
          {detail.task === "uplift" && (
            <div className="grid gap-3" style={{ gridTemplateColumns: "repeat(auto-fit,minmax(120px,1fr))" }}>
              <Stat label="Qini coefficient" value={fmt(m.qini_coefficient, 2)} tone="accent" />
              <Stat label="Treatment" value={fmt(m.n_treatment, 0)} />
              <Stat label="Control" value={fmt(m.n_control, 0)} />
            </div>
          )}

          {/* The confusion matrix, accuracy and calibration were COMPUTED AND PERSISTED and never
              rendered — while the sidebar entry for this page promises "accuracy, drift, and
              confusion matrix". Everything below reads keys already on the wire
              (model_evaluation.confusion_at / calibration_curve); no backend change was needed,
              which is what made the gap invisible. */}
          {detail.task === "binary" && m.confusion && (
            <div className="mt-4" data-testid="eval-confusion">
              <SectionTitle right={<Badge tone="neutral">threshold {fmt(m.confusion.threshold, 2)}</Badge>}>
                Confusion matrix
              </SectionTitle>
              <div className="grid gap-3" style={{ gridTemplateColumns: "repeat(auto-fit,minmax(120px,1fr))", marginBottom: 10 }}>
                <Stat label="Accuracy" value={fmt(m.confusion.accuracy)} tone="accent" />
                <Stat label="Precision" value={fmt(m.confusion.precision)} />
                <Stat label="Recall" value={fmt(m.confusion.recall)} />
                <Stat label="F1" value={fmt(m.confusion.f1)} />
              </div>
              <div style={{ overflowX: "auto" }}>
                <table style={{ fontSize: 13, borderCollapse: "collapse" }}>
                  <thead>
                    <tr>
                      <th style={{ padding: "4px 10px" }} />
                      <th style={{ padding: "4px 10px", color: "var(--rm-dim)" }}>Predicted 1</th>
                      <th style={{ padding: "4px 10px", color: "var(--rm-dim)" }}>Predicted 0</th>
                    </tr>
                  </thead>
                  <tbody>
                    {/* Rows are ACTUAL, columns PREDICTED, and each cell is labelled with its own
                        name — a confusion matrix whose orientation you have to guess is worse than
                        none, because a transposed reading swaps precision and recall. */}
                    <tr style={{ borderTop: "1px solid var(--rm-border)" }}>
                      <td style={{ padding: "4px 10px", color: "var(--rm-dim)" }}>Actual 1</td>
                      <td style={{ padding: "4px 10px", fontVariantNumeric: "tabular-nums" }}>{fmt(m.confusion.tp, 0)} <span style={{ color: "var(--rm-dim)" }}>TP</span></td>
                      <td style={{ padding: "4px 10px", fontVariantNumeric: "tabular-nums" }}>{fmt(m.confusion.fn, 0)} <span style={{ color: "var(--rm-dim)" }}>FN</span></td>
                    </tr>
                    <tr style={{ borderTop: "1px solid var(--rm-border)" }}>
                      <td style={{ padding: "4px 10px", color: "var(--rm-dim)" }}>Actual 0</td>
                      <td style={{ padding: "4px 10px", fontVariantNumeric: "tabular-nums" }}>{fmt(m.confusion.fp, 0)} <span style={{ color: "var(--rm-dim)" }}>FP</span></td>
                      <td style={{ padding: "4px 10px", fontVariantNumeric: "tabular-nums" }}>{fmt(m.confusion.tn, 0)} <span style={{ color: "var(--rm-dim)" }}>TN</span></td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {Array.isArray(m.calibration_curve) && m.calibration_curve.length > 0 && (
            <div className="mt-4" data-testid="eval-calibration">
              <SectionTitle>Calibration</SectionTitle>
              <table style={{ width: "100%", fontSize: 13, borderCollapse: "collapse" }}>
                <thead>
                  <tr>
                    {["Bin", "Mean predicted", "Observed", "Count"].map((h) => (
                      <th key={h} style={{ textAlign: "left", padding: "4px 8px", color: "var(--rm-dim)" }}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {m.calibration_curve.map((c: any, i: number) => (
                    <tr key={i} style={{ borderTop: "1px solid var(--rm-border)" }}>
                      <td style={{ padding: "4px 8px" }}>{i + 1}</td>
                      <td style={{ padding: "4px 8px", fontVariantNumeric: "tabular-nums" }}>{fmt(c.mean_predicted)}</td>
                      <td style={{ padding: "4px 8px", fontVariantNumeric: "tabular-nums" }}>{fmt(c.fraction_positive)}</td>
                      <td style={{ padding: "4px 8px", fontVariantNumeric: "tabular-nums" }}>{fmt(c.count, 0)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {Array.isArray(detail.gate_result?.checks) && detail.gate_result.checks.length > 0 && (
            <div className="mt-4">
              <SectionTitle>Promotion gate</SectionTitle>
              <table style={{ width: "100%", fontSize: 13, borderCollapse: "collapse" }}>
                <tbody>
                  {detail.gate_result.checks.map((c: any, i: number) => (
                    <tr key={i} style={{ borderTop: "1px solid var(--border,#eee)" }}>
                      <td style={{ padding: "4px 8px" }}>{c.check}</td>
                      <td style={{ padding: "4px 8px" }}>{fmt(c.value)}</td>
                      <td style={{ padding: "4px 8px", color: "var(--muted,#666)" }}>thr {fmt(c.threshold)}</td>
                      <td style={{ padding: "4px 8px" }}><Badge tone={c.passed ? "success" : "danger"}>{c.passed ? "pass" : "fail"}</Badge></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {Array.isArray(m.decile_table?.bands) && m.decile_table.bands.length > 0 && (
            <div className="mt-4">
              <SectionTitle right={<Badge tone={m.decile_table.monotonic ? "success" : "warning"}>{m.decile_table.monotonic ? "monotonic" : "non-monotonic"}</Badge>}>Decile rank-ordering</SectionTitle>
              <div style={{ overflowX: "auto" }}>
                <table style={{ width: "100%", fontSize: 13, borderCollapse: "collapse" }}>
                  <thead><tr style={{ textAlign: "left", color: "var(--muted,#666)" }}>
                    <th style={{ padding: "4px 8px" }}>Band</th><th>Count</th><th>Avg score</th><th>Events</th><th>Event rate</th><th>WoE</th>
                  </tr></thead>
                  <tbody>
                    {m.decile_table.bands.map((b: any) => (
                      <tr key={b.band} style={{ borderTop: "1px solid var(--border,#eee)" }}>
                        <td style={{ padding: "4px 8px" }}>{b.band}</td><td>{b.count}</td><td>{fmt(b.avg_score)}</td>
                        <td>{b.events}</td><td>{fmt(b.event_rate)}</td><td>{fmt(b.woe)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {Array.isArray(detail.temporal?.buckets) && detail.temporal.buckets.length > 0 && (
            <div className="mt-4">
              <SectionTitle>Temporal backtest (by {detail.temporal.by})</SectionTitle>
              <div style={{ overflowX: "auto" }}>
                <table style={{ width: "100%", fontSize: 13, borderCollapse: "collapse" }}>
                  <thead><tr style={{ textAlign: "left", color: "var(--muted,#666)" }}><th style={{ padding: "4px 8px" }}>Bucket</th><th>N</th><th>Base rate</th><th>Gini</th><th>KS</th></tr></thead>
                  <tbody>
                    {detail.temporal.buckets.map((b: any) => (
                      <tr key={b.bucket} style={{ borderTop: "1px solid var(--border,#eee)" }}>
                        <td style={{ padding: "4px 8px" }}>{b.bucket}</td><td>{b.n}</td><td>{fmt(b.base_rate)}</td><td>{fmt(b.gini)}</td><td>{fmt(b.ks)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {Array.isArray(detail.segments?.slices) && detail.segments.slices.length > 0 && (
            <div className="mt-4">
              <SectionTitle>Segments (by {detail.segments.by})</SectionTitle>
              <div style={{ overflowX: "auto" }}>
                <table style={{ width: "100%", fontSize: 13, borderCollapse: "collapse" }}>
                  <thead><tr style={{ textAlign: "left", color: "var(--muted,#666)" }}><th style={{ padding: "4px 8px" }}>Segment</th><th>N</th><th>Base rate</th><th>Gini</th><th>KS</th></tr></thead>
                  <tbody>
                    {detail.segments.slices.map((b: any) => (
                      <tr key={b.bucket} style={{ borderTop: "1px solid var(--border,#eee)" }}>
                        <td style={{ padding: "4px 8px" }}>{b.bucket}</td><td>{b.n}</td><td>{fmt(b.base_rate)}</td><td>{fmt(b.gini)}</td><td>{fmt(b.ks)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </Card>
      )}

      <Card>
        <SectionTitle>Past evaluations</SectionTitle>
        {evals.length === 0 ? (
          <EmptyState icon={<LineChart />} title="No evaluations yet" hint="Run one above to score a model against ground-truth labels." />
        ) : (
          <div className="space-y-1">
            {evals.map((e) => (
              <div key={e.id} className="flex items-center justify-between" style={{ padding: "6px 0", borderTop: "1px solid var(--border,#eee)" }}>
                <button onClick={() => open(e.id)} style={{ textAlign: "left", background: "none", border: "none", cursor: "pointer", color: "inherit" }}>
                  <strong>{e.name}</strong> <span style={{ color: "var(--muted,#666)" }}>· {e.task}</span>
                </button>
                <div className="flex items-center gap-2">
                  <Badge tone={e.gate_status === "pass" ? "success" : e.gate_status === "fail" ? "danger" : "neutral"}>{e.gate_status}</Badge>
                  <Button variant="ghost" onClick={() => remove(e.id)}><Trash2 size={14} /></Button>
                </div>
              </div>
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}
