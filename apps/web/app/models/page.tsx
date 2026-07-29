"use client";

import * as React from "react";
import { Upload, Trash2, Play, Boxes } from "lucide-react";
import { apiJson } from "../../src/lib/api";
import { useRuleMindStore } from "../../src/lib/store";
import { Button, Card, Field, Input, Select, Textarea, Badge, EmptyState, PageHeader, SectionTitle } from "../../src/v3/ui";

type Model = { id: string; name: string; model_type?: string; status?: string; description?: string };

// Read a file to base64 (no data: prefix) for the model_base64 upload field.
function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const res = String(reader.result);
      resolve(res.includes(",") ? res.split(",")[1] : res);
    };
    reader.onerror = () => reject(new Error("Could not read the file."));
    reader.readAsDataURL(file);
  });
}

export default function ModelsPage() {
  const { apiBaseUrl, apiKey } = useRuleMindStore();
  const [models, setModels] = React.useState<Model[]>([]);
  const [error, setError] = React.useState<string | null>(null);
  const [status, setStatus] = React.useState<string | null>(null);

  const [name, setName] = React.useState("");
  const [description, setDescription] = React.useState("");
  const [modelType, setModelType] = React.useState("sklearn");
  const [fileName, setFileName] = React.useState<string | null>(null);
  const [base64, setBase64] = React.useState<string>("");
  const [uploading, setUploading] = React.useState(false);

  const [predictId, setPredictId] = React.useState<string | null>(null);
  // Flat feature→value map (one row). The executor builds a single input row from
  // these values in order — a nested array like {"features":[...]} would be read as
  // an extra dimension and rejected by the model.
  const [predictInput, setPredictInput] = React.useState('{\n  "feature_0": 0.9,\n  "feature_1": 0.1,\n  "feature_2": 0.2,\n  "feature_3": 0.3\n}');
  const [predictOut, setPredictOut] = React.useState<string | null>(null);

  const load = React.useCallback(async () => {
    try {
      setModels(await apiJson<Model[]>(apiBaseUrl, "/api/v1/models", {}, apiKey));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Unable to load models.");
    }
  }, [apiBaseUrl, apiKey]);

  React.useEffect(() => { void load(); }, [load]);

  const onFile = async (file: File) => {
    setFileName(file.name);
    if (!name) setName(file.name.replace(/\.(pkl|joblib|bin)$/i, ""));
    try { setBase64(await fileToBase64(file)); } catch (e) { setError(e instanceof Error ? e.message : "File read failed."); }
  };

  const upload = async () => {
    if (!name.trim() || !base64) { setError("Give the model a name and choose a .pkl file."); return; }
    setUploading(true); setError(null); setStatus(null);
    try {
      await apiJson(apiBaseUrl, "/api/v1/models", { method: "POST", body: JSON.stringify({ name: name.trim(), description, model_type: modelType, model_base64: base64, status: "dev" }) }, apiKey);
      setStatus(`Uploaded “${name}”.`);
      setName(""); setDescription(""); setFileName(null); setBase64("");
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Upload failed.");
    } finally {
      setUploading(false);
    }
  };

  const del = async (id: string) => {
    try { await apiJson(apiBaseUrl, `/api/v1/models/${id}`, { method: "DELETE" }, apiKey); await load(); }
    catch (e) { setError(e instanceof Error ? e.message : "Delete failed."); }
  };

  const runPredict = async (id: string) => {
    setPredictOut(null);
    let input: unknown;
    try { input = JSON.parse(predictInput); } catch { setPredictOut("Invalid JSON input."); return; }
    try {
      const res = await apiJson(apiBaseUrl, `/api/v1/models/${id}/predict`, { method: "POST", body: JSON.stringify({ input_data: input }) }, apiKey);
      setPredictOut(JSON.stringify(res, null, 2));
    } catch (e) {
      setPredictOut(e instanceof Error ? e.message : "Predict failed.");
    }
  };

  return (
    <div style={{ padding: 24, display: "grid", gap: 18 }}>
      <PageHeader title="ML Models" subtitle="Upload, host, and run Python models (.pkl / joblib) as policy steps — predict and probability outputs, testable inline." />

      {error ? <div style={{ padding: 12, borderRadius: 10, background: "var(--rm-danger-bg)", color: "var(--rm-danger)", fontSize: 13 }}>{error}</div> : null}
      {status ? <div style={{ padding: 12, borderRadius: 10, background: "var(--rm-success-bg)", color: "var(--rm-success)", fontSize: 13 }}>{status}</div> : null}

      <div style={{ display: "grid", gridTemplateColumns: "minmax(320px, 380px) minmax(0,1fr)", gap: 18, alignItems: "start" }}>
        {/* upload */}
        <Card>
          <SectionTitle>Upload model</SectionTitle>
          <div style={{ display: "grid", gap: 14 }}>
            <Field label="Name"><Input value={name} onChange={(e) => setName(e.target.value)} placeholder="Risk classifier v2" /></Field>
            <Field label="Type">
              <Select value={modelType} onChange={(e) => setModelType(e.target.value)}>
                {["sklearn", "xgboost", "lightgbm", "custom"].map((t) => <option key={t} value={t}>{t}</option>)}
              </Select>
            </Field>
            <Field label="Description"><Input value={description} onChange={(e) => setDescription(e.target.value)} placeholder="Optional" /></Field>
            <Field label="Model file (.pkl / .joblib)">
              <label>
                <input type="file" accept=".pkl,.joblib,.bin" style={{ display: "none" }} onChange={(e) => { const f = e.target.files?.[0]; if (f) onFile(f); }} />
                <span className="rm-btn rm-btn-secondary" style={{ width: "100%", cursor: "pointer" }}><Upload size={14} /> {fileName ?? "Choose file"}</span>
              </label>
            </Field>
            <Button variant="primary" onClick={upload} disabled={uploading || !base64}>{uploading ? "Uploading…" : "Upload model"}</Button>
          </div>
        </Card>

        {/* list */}
        <div style={{ display: "grid", gap: 12 }}>
          {models.length === 0 ? (
            <Card><EmptyState icon={<Boxes size={22} />} title="No models yet" hint="Upload a pickled scikit-learn / xgboost model to host it and use it as a policy step." /></Card>
          ) : null}
          {models.map((m) => (
            <Card key={m.id} style={{ display: "grid", gap: 10 }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 12 }}>
                <div>
                  <div style={{ fontSize: 15, fontWeight: 700, color: "var(--rm-text)" }}>{m.name}</div>
                  <div style={{ fontSize: 12.5, color: "var(--rm-dim)", marginTop: 2 }}>{m.description || "—"}</div>
                </div>
                <div style={{ display: "flex", gap: 6 }}>
                  <Badge tone="accent">{m.model_type ?? "model"}</Badge>
                  <Badge tone={m.status === "prod" ? "success" : "neutral"}>{m.status ?? "dev"}</Badge>
                </div>
              </div>
              <div style={{ display: "flex", gap: 8 }}>
                <Button variant="secondary" size="sm" onClick={() => { setPredictId(predictId === m.id ? null : m.id); setPredictOut(null); }}><Play size={13} /> Predict</Button>
                <Button variant="ghost" size="sm" onClick={() => del(m.id)}><Trash2 size={13} /> Delete</Button>
              </div>
              {predictId === m.id ? (
                <div style={{ display: "grid", gap: 8, borderTop: "1px solid var(--rm-border)", paddingTop: 10 }}>
                  <div style={{ fontSize: 11.5, color: "var(--rm-dim)" }}>Flat feature → value map (one row, values used in order).</div>
                  <Textarea mono rows={4} value={predictInput} onChange={(e) => setPredictInput(e.target.value)} style={{ fontSize: 12 }} />
                  <Button variant="primary" size="sm" onClick={() => runPredict(m.id)} style={{ justifySelf: "start" }}>Run predict</Button>
                  {predictOut ? <pre style={{ margin: 0, padding: 10, background: "var(--rm-editor)", border: "1px solid var(--rm-border)", borderRadius: 8, fontFamily: "var(--font-mono)", fontSize: 12, color: "var(--rm-code-text)", whiteSpace: "pre-wrap", maxHeight: 180, overflow: "auto" }}>{predictOut}</pre> : null}
                </div>
              ) : null}
            </Card>
          ))}
        </div>
      </div>
    </div>
  );
}
