"use client";

import * as React from "react";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type PropsWithChildren
} from "react";
import type {
  EnvironmentName,
  EvaluationResult,
  FieldType,
  RuleConnection,
  RuleDefinition,
  RuleNode,
  RuleRecord,
  RuleVersionRecord,
  ValidationIssue
} from "@rulemind/shared";
import {
  FTYPES,
  ICONS,
  NODE_DEFS,
  OPS,
  PROTOTYPE_THEME,
  cloneDefinition,
  createLocalRuleRecord,
  createLocalVersionRecord,
  emptyDefinition,
  evaluatePrototypeRule,
  fieldInputType,
  generatePrototypeExpression,
  nodeColors,
  nodeHeight,
  nodeWidth,
  prototypeResultToEvaluation,
  type PrototypeThemeColors,
  type ThemeMode,
  validatePrototypeRule
} from "./prototype-spec";

type RuleBuilderLoadPayload =
  | RuleRecord
  | RuleDefinition
  | {
      id?: string;
      name?: string;
      expression?: string;
      nodes?: RuleNode[];
      connections?: RuleConnection[];
      metadata?: RuleDefinition["metadata"];
    };

export interface RuleBuilderProps {
  environment?: EnvironmentName;
  onEnvironmentChange?: (environment: EnvironmentName) => void;
  initialRule?: RuleDefinition;
  theme?: ThemeMode;
  readOnly?: boolean;
  requireSavedRuleForEvaluation?: boolean;
  onLoad?: (
    payload: RuleDefinition & {
      id?: string;
      name?: string;
      expression?: string;
    }
  ) => Promise<RuleBuilderLoadPayload | null | undefined> | RuleBuilderLoadPayload | null | undefined | void;
  onSave?: (payload: {
    id?: string;
    name: string;
    environment: EnvironmentName;
    definition: RuleDefinition;
    tags: string[];
    nodes: RuleNode[];
    connections: RuleConnection[];
    expression: string;
  }) => Promise<{ id: string; version: number } | void> | { id: string; version: number } | void;
  onListRules?: (environment: EnvironmentName) => Promise<RuleRecord[]>;
  onLoadVersions?: (ruleId: string) => Promise<RuleVersionRecord[]>;
  onEvaluate?: (payload: {
    ruleId?: string;
    definition: RuleDefinition;
    input: Record<string, unknown>;
  }) => Promise<EvaluationResult>;
  onValidate?: (definition: RuleDefinition) => Promise<{
    issues: ValidationIssue[];
    expression?: string;
  }>;
}

interface ThemeContextValue {
  mode: ThemeMode;
  t: PrototypeThemeColors;
  setMode: React.Dispatch<React.SetStateAction<ThemeMode>>;
}

const ThemeCtx = createContext<ThemeContextValue | null>(null);

function useTheme() {
  const context = useContext(ThemeCtx);

  if (!context) {
    throw new Error("ThemeCtx missing.");
  }

  return context;
}

function ThemeProvider({ children, initialMode = "dark" }: PropsWithChildren<{ initialMode?: ThemeMode }>) {
  const [mode, setMode] = useState<ThemeMode>(initialMode);

  useEffect(() => {
    setMode(initialMode);
  }, [initialMode]);

  return <ThemeCtx.Provider value={{ mode, t: PROTOTYPE_THEME[mode], setMode }}>{children}</ThemeCtx.Provider>;
}

let nodeIdCounter = 0;
function createNodeId() {
  return `n${++nodeIdCounter}_${Date.now().toString(36)}`;
}

function useUndoState<T>(initialValue: T) {
  const [state, setState] = useState(initialValue);
  const history = useRef<T[]>([]);

  const commit = useCallback((value: T | ((current: T) => T)) => {
    setState((current) => {
      history.current.push(structuredClone(current));
      if (history.current.length > 50) {
        history.current.shift();
      }
      return typeof value === "function" ? (value as (current: T) => T)(current) : value;
    });
  }, []);

  const undo = useCallback(() => {
    if (history.current.length === 0) {
      return;
    }

    const previous = history.current.pop();
    if (previous !== undefined) {
      setState(previous);
    }
  }, []);

  return [state, commit, undo] as const;
}

function useDebouncedEffect(callback: () => void | Promise<void>, dependencies: unknown[], delayMs: number) {
  useEffect(() => {
    const timer = window.setTimeout(() => {
      void callback();
    }, delayMs);

    return () => window.clearTimeout(timer);
  }, dependencies);
}

function ensurePrototypeFonts() {
  if (typeof document === "undefined") {
    return;
  }

  if (document.querySelector('link[data-rulemind-fonts="true"]')) {
    return;
  }

  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href =
    "https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap";
  link.setAttribute("data-rulemind-fonts", "true");
  document.head.appendChild(link);
}

function normalizeLoadedRule(payload: RuleBuilderLoadPayload) {
  if ("definition" in payload && payload.definition) {
    return {
      id: "id" in payload && typeof payload.id === "string" ? payload.id : undefined,
      name: "name" in payload && typeof payload.name === "string" ? payload.name : payload.definition.metadata?.name ?? "",
      definition: cloneDefinition(payload.definition),
      expression: "expression" in payload && typeof payload.expression === "string"
        ? payload.expression
        : generatePrototypeExpression(payload.definition.nodes, payload.definition.connections)
    };
  }

  const source = payload as {
    id?: string;
    name?: string;
    expression?: string;
    nodes?: RuleNode[];
    connections?: RuleConnection[];
    metadata?: RuleDefinition["metadata"];
  };
  const definition: RuleDefinition = {
    nodes: Array.isArray(source.nodes) ? cloneDefinition({ nodes: source.nodes, connections: [], metadata: source.metadata }).nodes : [],
    connections: Array.isArray(source.connections) ? structuredClone(source.connections) : [],
    metadata: source.metadata
  };

  return {
    id: typeof source.id === "string" ? source.id : undefined,
    name: typeof source.name === "string" ? source.name : source.metadata?.name ?? "",
    definition,
    expression: typeof source.expression === "string"
      ? source.expression
      : generatePrototypeExpression(definition.nodes, definition.connections)
  };
}

const S = {
  input: (t: PrototypeThemeColors) => ({
    width: "100%",
    padding: "7px 10px",
    background: t.bg3,
    border: `1px solid ${t.border}`,
    borderRadius: 6,
    color: t.tx,
    fontSize: 13,
    fontFamily: "'JetBrains Mono',monospace",
    outline: "none",
    boxSizing: "border-box" as const
  }),
  label: (t: PrototypeThemeColors) => ({
    fontSize: 10,
    fontWeight: 700,
    color: t.tx3,
    marginBottom: 3,
    display: "block",
    textTransform: "uppercase" as const,
    letterSpacing: ".06em"
  }),
  select: (t: PrototypeThemeColors) => ({
    width: "100%",
    padding: "7px 10px",
    background: t.bg3,
    border: `1px solid ${t.border}`,
    borderRadius: 6,
    color: t.tx,
    fontSize: 13,
    fontFamily: "'DM Sans',sans-serif",
    outline: "none",
    boxSizing: "border-box" as const,
    appearance: "auto" as const
  }),
  btn: (t: PrototypeThemeColors, primary: boolean) => ({
    padding: "6px 16px",
    fontSize: 13,
    borderRadius: 7,
    cursor: "pointer",
    fontWeight: 600,
    fontFamily: "'DM Sans',sans-serif",
    border: primary ? "none" : `1px solid ${t.border}`,
    background: primary ? t.acc : "transparent",
    color: primary ? "#fff" : t.tx2
  })
};

function Button({
  children,
  primary,
  disabled,
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & { primary?: boolean }) {
  const { t } = useTheme();
  return (
    <button
      {...props}
      disabled={disabled}
      style={{
        ...S.btn(t, Boolean(primary)),
        opacity: disabled ? 0.4 : 1,
        cursor: disabled ? "not-allowed" : "pointer",
        ...(props.style ?? {})
      }}
    >
      {children}
    </button>
  );
}

function ConfigPanel({
  node,
  onUpdate,
  onDelete,
  onClose,
  readOnly
}: {
  node: RuleNode | null;
  onUpdate: (nodeId: string, config: RuleNode["config"], label?: string) => void;
  onDelete: (nodeId: string) => void;
  onClose: () => void;
  readOnly?: boolean;
}) {
  const { t } = useTheme();

  if (!node) {
    return null;
  }

  const config = node.config || {};
  const updateConfig = (key: string, value: unknown) => onUpdate(node.id, { ...config, [key]: value });
  const isConditional = node.type === "condition" || node.type === "score";
  const needsValue = !["exists", "!exists"].includes(String(config.operator));

  return (
    <div
      data-testid="config-panel"
      style={{
        position: "absolute",
        top: 10,
        right: 10,
        width: 280,
        background: t.bgEl,
        border: `1px solid ${t.border}`,
        borderRadius: 12,
        padding: 14,
        zIndex: 100,
        boxShadow: t.sh,
        fontFamily: "'DM Sans',sans-serif"
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 14 }}>
        <span style={{ fontSize: 13, fontWeight: 700, color: t.tx }}>Configure: {node.label}</span>
        <button
          data-testid="config-close"
          onClick={onClose}
          style={{ background: "none", border: "none", color: t.tx3, cursor: "pointer", fontSize: 16, lineHeight: 1 }}
        >
          {"\u2715"}
        </button>
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        <div>
          <label style={S.label(t)}>Display Label</label>
          <input
            data-testid="config-label"
            value={node.label || ""}
            disabled={readOnly}
            onChange={(event) => onUpdate(node.id, config, event.target.value)}
            style={S.input(t)}
          />
        </div>
        {isConditional ? (
          <>
            <div>
              <label style={S.label(t)}>Field Type</label>
              <select
                data-testid="config-field-type"
                value={String(config.fieldType || (node.type === "score" ? "number" : "number"))}
                disabled={readOnly}
                onChange={(event) => updateConfig("fieldType", event.target.value)}
                style={S.select(t)}
              >
                {FTYPES.map((fieldType) => (
                  <option key={fieldType.v} value={fieldType.v}>
                    {fieldType.l}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label style={S.label(t)}>Field Name</label>
              <input
                data-testid="config-field-name"
                value={String(config.field || "")}
                disabled={readOnly}
                onChange={(event) => updateConfig("field", event.target.value)}
                placeholder="e.g. credit_score"
                style={S.input(t)}
              />
            </div>
            <div>
              <label style={S.label(t)}>Operator</label>
              <select
                data-testid="config-operator"
                value={String(config.operator || "==")}
                disabled={readOnly}
                onChange={(event) => updateConfig("operator", event.target.value)}
                style={S.select(t)}
              >
                {OPS.map((operator) => (
                  <option key={operator.v} value={operator.v}>
                    {operator.l} ({operator.v})
                  </option>
                ))}
              </select>
            </div>
            {needsValue ? (
              <div data-testid="config-value-wrapper">
                <label style={S.label(t)}>Value</label>
                <input
                  data-testid="config-value"
                  value={String(config.value ?? "")}
                  disabled={readOnly}
                  onChange={(event) => updateConfig("value", event.target.value)}
                  type={String(config.fieldType) === "number" ? "number" : "text"}
                  placeholder={
                    String(config.fieldType) === "number"
                      ? "e.g. 700"
                      : String(config.fieldType) === "boolean"
                        ? "true / false"
                        : "value"
                  }
                  style={S.input(t)}
                />
              </div>
            ) : null}
            {String(config.operator) === "between" ? (
              <div data-testid="config-upper-bound-wrapper">
                <label style={S.label(t)}>Upper Bound</label>
                <input
                  data-testid="config-upper-bound"
                  value={String(config.value2 ?? "")}
                  disabled={readOnly}
                  onChange={(event) => updateConfig("value2", event.target.value)}
                  type="number"
                  placeholder="e.g. 850"
                  style={S.input(t)}
                />
              </div>
            ) : null}
            {(String(config.operator) === "in" || String(config.operator) === "not_in") ? (
              <div style={{ fontSize: 11, color: t.tx3, marginTop: -4 }}>Comma-separated: val1, val2, val3</div>
            ) : null}
          </>
        ) : null}
        {node.type === "trigger" ? (
          <div>
            <label style={S.label(t)}>Event Name</label>
            <input
              data-testid="config-event"
              value={String(config.event || "")}
              disabled={readOnly}
              onChange={(event) => updateConfig("event", event.target.value)}
              placeholder="on_application_submit"
              style={S.input(t)}
            />
          </div>
        ) : null}
        {node.type === "group" ? (
          <div>
            <label style={S.label(t)}>Group Operator</label>
            <select
              data-testid="config-group-op"
              value={String(config.groupOp || "AND")}
              disabled={readOnly}
              onChange={(event) => updateConfig("groupOp", event.target.value)}
              style={S.select(t)}
            >
              <option value="AND">AND</option>
              <option value="OR">OR</option>
            </select>
          </div>
        ) : null}
        {(node.type === "approve" || node.type === "review" || node.type === "reject") ? (
          <div>
            <label style={S.label(t)}>Reason</label>
            <input
              data-testid="config-reason"
              value={String(config.reason || "")}
              disabled={readOnly}
              onChange={(event) => updateConfig("reason", event.target.value)}
              placeholder="Optional reason"
              style={S.input(t)}
            />
          </div>
        ) : null}
      </div>
      <button
        data-testid="config-delete-node"
        onClick={() => onDelete(node.id)}
        disabled={readOnly}
        style={{
          marginTop: 14,
          width: "100%",
          padding: "7px 0",
          fontSize: 12,
          fontWeight: 600,
          background: t.mode === "dark" ? "#2a0a0a" : "#fee2e2",
          color: t.mode === "dark" ? "#f87171" : "#dc2626",
          border: `1px solid ${t.mode === "dark" ? "#5c1010" : "#fca5a5"}`,
          borderRadius: 7,
          cursor: readOnly ? "not-allowed" : "pointer",
          opacity: readOnly ? 0.45 : 1,
          fontFamily: "'DM Sans',sans-serif"
        }}
      >
        Delete Node
      </button>
    </div>
  );
}

function Canvas({
  nodes,
  setNodes,
  connections,
  setConnections,
  readOnly
}: {
  nodes: RuleNode[];
  setNodes: React.Dispatch<React.SetStateAction<RuleNode[]>>;
  connections: RuleConnection[];
  setConnections: React.Dispatch<React.SetStateAction<RuleConnection[]>>;
  readOnly?: boolean;
}) {
  const { t, mode } = useTheme();
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [connectSourceId, setConnectSourceId] = useState<string | null>(null);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const isPanning = useRef(false);
  const panStart = useRef({ x: 0, y: 0, px: 0, py: 0 });
  const [draggingNodeId, setDraggingNodeId] = useState<string | null>(null);
  const dragOffset = useRef({ x: 0, y: 0 });

  const onBackgroundDown = (event: React.MouseEvent<HTMLDivElement>) => {
    if (event.target !== wrapRef.current || readOnly) {
      return;
    }

    setSelectedNodeId(null);
    setConnectSourceId(null);
    isPanning.current = true;
    panStart.current = { x: event.clientX, y: event.clientY, px: pan.x, py: pan.y };
  };

  useEffect(() => {
    const handleMove = (event: MouseEvent) => {
      if (isPanning.current) {
        setPan({
          x: panStart.current.px + (event.clientX - panStart.current.x),
          y: panStart.current.py + (event.clientY - panStart.current.y)
        });
      }
    };

    const handleUp = () => {
      isPanning.current = false;
    };

    window.addEventListener("mousemove", handleMove);
    window.addEventListener("mouseup", handleUp);
    return () => {
      window.removeEventListener("mousemove", handleMove);
      window.removeEventListener("mouseup", handleUp);
    };
  }, []);

  const startNodeDrag = (id: string, event: React.MouseEvent<HTMLDivElement>) => {
    if (readOnly) {
      return;
    }

    event.stopPropagation();
    const node = nodes.find((item) => item.id === id);
    if (!node) {
      return;
    }

    setDraggingNodeId(id);
    setSelectedNodeId(id);
    dragOffset.current = { x: event.clientX - node.x - pan.x, y: event.clientY - node.y - pan.y };
  };

  useEffect(() => {
    if (!draggingNodeId) {
      return;
    }

    const panSnapshot = { x: pan.x, y: pan.y };
    const handleMove = (event: MouseEvent) => {
      const nextX = event.clientX - dragOffset.current.x - panSnapshot.x;
      const nextY = event.clientY - dragOffset.current.y - panSnapshot.y;
      setNodes((current) => current.map((node) => (node.id === draggingNodeId ? { ...node, x: nextX, y: nextY } : node)));
    };
    const handleUp = () => setDraggingNodeId(null);

    window.addEventListener("mousemove", handleMove);
    window.addEventListener("mouseup", handleUp);
    return () => {
      window.removeEventListener("mousemove", handleMove);
      window.removeEventListener("mouseup", handleUp);
    };
  }, [draggingNodeId, pan.x, pan.y, setNodes]);

  const onDrop = (event: React.DragEvent<HTMLDivElement>) => {
    event.preventDefault();

    if (readOnly || !wrapRef.current) {
      return;
    }

    const type = event.dataTransfer.getData("nodeType") as RuleNode["type"];
    if (!type) {
      return;
    }

    const rect = wrapRef.current.getBoundingClientRect();
    const x = event.clientX - rect.left - pan.x;
    const y = event.clientY - rect.top - pan.y;
    const definition = NODE_DEFS.find((item) => item.type === type) || { label: type };
    const newNode: RuleNode = {
      id: createNodeId(),
      type,
      label: definition.label,
      x,
      y,
      config:
        type === "condition" || type === "score"
          ? { fieldType: type === "score" ? "number" : "string", field: "", operator: "==", value: "" }
          : type === "trigger"
            ? { event: "" }
            : type === "group"
              ? { groupOp: "AND" }
              : {}
    };
    setNodes((current) => [...current, newNode]);
    setSelectedNodeId(newNode.id);
  };

  const onNodeClick = (id: string, event: React.MouseEvent<HTMLDivElement>) => {
    event.stopPropagation();

    if (readOnly) {
      return;
    }

    if (connectSourceId && !readOnly) {
      if (connectSourceId !== id) {
        const exists = connections.some((connection) => connection.from === connectSourceId && connection.to === id);
        if (!exists) {
          setConnections((current) => [...current, { from: connectSourceId, to: id }]);
        }
      }
      setConnectSourceId(null);
      return;
    }

    setSelectedNodeId(id);
  };

  const updateNode = (id: string, config: RuleNode["config"], label?: string) => {
    setNodes((current) =>
      current.map((node) => (node.id === id ? { ...node, config, ...(label !== undefined ? { label } : {}) } : node))
    );
  };

  const deleteNode = (id: string) => {
    setNodes((current) => current.filter((node) => node.id !== id));
    setConnections((current) => current.filter((connection) => connection.from !== id && connection.to !== id));
    setSelectedNodeId(null);
  };

  const deleteConnection = (from: string, to: string) => {
    if (readOnly) {
      return;
    }

    setConnections((current) => current.filter((connection) => !(connection.from === from && connection.to === to)));
  };

  useEffect(() => {
    const handleKey = (event: KeyboardEvent) => {
      const activeElement = document.activeElement;
      const tagName = activeElement?.tagName;

      if (event.key === "Escape") {
        setSelectedNodeId(null);
        setConnectSourceId(null);
      }

      if (["INPUT", "SELECT", "TEXTAREA"].includes(String(tagName))) {
        return;
      }

      if ((event.key === "Delete" || event.key === "Backspace") && selectedNodeId && !readOnly) {
        deleteNode(selectedNodeId);
      }
    };

    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, [readOnly, selectedNodeId]);

  const selectedNode = nodes.find((node) => node.id === selectedNodeId) || null;

  return (
    <div style={{ flex: 1, display: "flex", flexDirection: "column", overflow: "hidden" }}>
      <div
        style={{
          padding: "10px 14px",
          display: "flex",
          gap: 6,
          flexWrap: "wrap",
          alignItems: "center",
          borderBottom: `1px solid ${t.border}`,
          background: t.bg2,
          flexShrink: 0
        }}
      >
        {NODE_DEFS.map((nodeDef) => {
          const color = nodeColors(nodeDef.type, mode === "dark");
          return (
            <div
              key={nodeDef.type}
              data-testid={`toolbar-node-${nodeDef.type}`}
              draggable={!readOnly}
              onDragStart={(event) => event.dataTransfer.setData("nodeType", nodeDef.type)}
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 5,
                padding: "5px 11px",
                background: color.bg,
                color: color.tx,
                border: `1px solid ${color.bd}`,
                borderRadius: 7,
                cursor: readOnly ? "default" : "grab",
                fontSize: 12,
                fontWeight: 600,
                userSelect: "none",
                fontFamily: "'DM Sans',sans-serif",
                whiteSpace: "nowrap"
              }}
            >
              <span style={{ fontSize: 11 }}>{ICONS[nodeDef.type]}</span>
              {nodeDef.label}
            </div>
          );
        })}
        <div style={{ flex: 1 }} />
        {connectSourceId ? <span style={{ fontSize: 12, color: t.acc, fontStyle: "italic" }}>Click target node · Esc to cancel</span> : null}
        <button
          data-testid="connect-button"
          onClick={() => (connectSourceId ? setConnectSourceId(null) : selectedNodeId ? setConnectSourceId(selectedNodeId) : null)}
          disabled={(!selectedNodeId && !connectSourceId) || readOnly}
          style={{
            ...S.btn(t, Boolean(connectSourceId || selectedNodeId)),
            fontSize: 12,
            padding: "4px 12px",
            opacity: (!selectedNodeId && !connectSourceId) || readOnly ? 0.4 : 1,
            cursor: (!selectedNodeId && !connectSourceId) || readOnly ? "not-allowed" : "pointer"
          }}
        >
          {connectSourceId ? "Cancel" : "Connect →"}
        </button>
      </div>

      <div
        style={{
          flex: 1,
          position: "relative",
          overflow: "hidden",
          cursor: isPanning.current ? "grabbing" : connectSourceId ? "crosshair" : "default"
        }}
      >
        <div
          ref={wrapRef}
          data-testid="canvas-surface"
          onMouseDown={onBackgroundDown}
          onDrop={onDrop}
          onDragOver={(event) => event.preventDefault()}
          style={{
            width: "100%",
            height: "100%",
            position: "relative",
            background: t.canvas,
            backgroundImage: `radial-gradient(${t.dot} 1px, transparent 1px)`,
            backgroundSize: "24px 24px",
            backgroundPosition: `${pan.x % 24}px ${pan.y % 24}px`
          }}
        >
          <svg style={{ position: "absolute", top: 0, left: 0, width: "100%", height: "100%", pointerEvents: "none", zIndex: 5 }}>
            <defs>
              <marker id="ah" markerWidth="8" markerHeight="6" refX="7" refY="3" orient="auto">
                <polygon points="0 0, 8 3, 0 6" fill={t.tx3} />
              </marker>
            </defs>
            {connections.map((connection) => {
              const fromNode = nodes.find((node) => node.id === connection.from);
              const toNode = nodes.find((node) => node.id === connection.to);

              if (!fromNode || !toNode) {
                return null;
              }

              const fromWidth = nodeWidth(fromNode);
              const fromHeight = nodeHeight(fromNode);
              const toHeight = nodeHeight(toNode);
              const x1 = fromNode.x + pan.x + fromWidth;
              const y1 = fromNode.y + pan.y + fromHeight / 2;
              const x2 = toNode.x + pan.x;
              const y2 = toNode.y + pan.y + toHeight / 2;
              const dx = Math.max(Math.abs(x2 - x1) * 0.4, 30);
              const path = `M${x1} ${y1} C${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`;

              return (
                <g key={`${connection.from}-${connection.to}`} data-testid={`connection-${connection.from}-${connection.to}`}>
                  <path d={path} stroke={t.tx3} strokeWidth={1.5} fill="none" markerEnd="url(#ah)" opacity={0.55} />
                  <path
                    d={path}
                    stroke="transparent"
                    strokeWidth={14}
                    fill="none"
                    style={{ pointerEvents: "stroke", cursor: readOnly ? "default" : "pointer" }}
                    onClick={() => deleteConnection(connection.from, connection.to)}
                  />
                </g>
              );
            })}
          </svg>

          {nodes.map((node) => {
            const color = nodeColors(node.type, mode === "dark");
            const isSelected = selectedNodeId === node.id;
            const isConnectionSource = connectSourceId === node.id;
            const conditionText =
              node.type === "condition" || node.type === "score"
                ? (() => {
                    const config = node.config || {};
                    if (!config.field) {
                      return null;
                    }
                    const operator = String(config.operator || "==");
                    if (operator === "exists" || operator === "!exists") {
                      return `${config.field} ${operator}`;
                    }
                    if (operator === "between") {
                      return `${config.field} ∈ [${config.value || "?"}, ${config.value2 || "?"}]`;
                    }
                    return `${config.field} ${operator} ${config.value ?? "?"}`;
                  })()
                : null;

            return (
              <div
                key={node.id}
                data-testid={`canvas-node-${node.id}`}
                onMouseDown={(event) => startNodeDrag(node.id, event)}
                onClick={(event) => onNodeClick(node.id, event)}
                style={{
                  position: "absolute",
                  left: node.x + pan.x,
                  top: node.y + pan.y,
                  background: color.bg,
                  color: color.tx,
                  border: `2px solid ${isConnectionSource ? t.acc : isSelected ? t.acc : color.bd}`,
                  borderRadius: 9,
                  padding: "7px 14px",
                  cursor: draggingNodeId === node.id ? "grabbing" : readOnly ? "default" : "grab",
                  display: "flex",
                  flexDirection: "column",
                  gap: 1,
                  alignItems: "flex-start",
                  minWidth: 90,
                  zIndex: isSelected ? 20 : 10,
                  boxShadow: isSelected ? `0 0 0 3px ${t.acc}33, ${t.sh}` : t.sh,
                  transition: draggingNodeId === node.id ? "none" : "box-shadow .12s",
                  fontFamily: "'DM Sans',sans-serif",
                  userSelect: "none"
                }}
              >
                <div style={{ display: "flex", alignItems: "center", gap: 5, fontSize: 12.5, fontWeight: 700 }}>
                  <span style={{ fontSize: 12 }}>{ICONS[node.type]}</span>
                  {node.label}
                </div>
                {conditionText ? (
                  <div style={{ fontSize: 10, opacity: 0.75, fontFamily: "'JetBrains Mono',monospace", marginTop: 1 }}>
                    {conditionText}
                  </div>
                ) : null}
                {node.type === "group" ? <div style={{ fontSize: 10, opacity: 0.6 }}>{String(node.config?.groupOp || "AND")}</div> : null}
                <div
                  style={{
                    position: "absolute",
                    left: -5,
                    top: "50%",
                    transform: "translateY(-50%)",
                    width: 8,
                    height: 8,
                    borderRadius: "50%",
                    background: color.tx,
                    border: `2px solid ${color.bg}`
                  }}
                />
                <div
                  style={{
                    position: "absolute",
                    right: -5,
                    top: "50%",
                    transform: "translateY(-50%)",
                    width: 8,
                    height: 8,
                    borderRadius: "50%",
                    background: color.tx,
                    border: `2px solid ${color.bg}`
                  }}
                />
              </div>
            );
          })}

          {nodes.length === 0 ? (
            <div
              style={{
                position: "absolute",
                top: "50%",
                left: "50%",
                transform: "translate(-50%,-50%)",
                textAlign: "center",
                color: t.tx3,
                fontFamily: "'DM Sans',sans-serif",
                pointerEvents: "none"
              }}
            >
              <div style={{ fontSize: 40, marginBottom: 10, opacity: 0.25 }}>{"\u229E"}</div>
              <div style={{ fontSize: 14, fontWeight: 600 }}>Drag nodes from the toolbar above</div>
              <div style={{ fontSize: 12, marginTop: 6, maxWidth: 340, lineHeight: 1.6 }}>
                Select a node then click "Connect →" to wire them together. Click on a connection line to delete it. Drag canvas background to pan. Press Delete to remove selected node. Ctrl+Z to undo.
              </div>
            </div>
          ) : null}
        </div>

        {selectedNode ? (
          <ConfigPanel
            node={selectedNode}
            onUpdate={updateNode}
            onDelete={deleteNode}
            onClose={() => setSelectedNodeId(null)}
            readOnly={readOnly}
          />
        ) : null}
      </div>
    </div>
  );
}

function highlightExpression(expression: string) {
  const { t } = useTheme();

  return expression.split("\n").map((line, index) => {
    if (/^\s*\/\//.test(line)) {
      return (
        <div key={index} style={{ color: t.tx3, fontStyle: "italic" }}>
          {line}
        </div>
      );
    }

    const colored = line
      .replace(/(AND|OR|NOT|WHEN|BETWEEN|IN|NOT IN|EXISTS|NOT EXISTS|MATCHES)\b/g, "\u0001kw\u0002$1\u0001/kw\u0002")
      .replace(/(APPROVE|REVIEW|REJECT)/g, "\u0001out\u0002$1\u0001/out\u0002")
      .replace(/(>=|<=|!=|==|=>|>|<)/g, "\u0001op\u0002$1\u0001/op\u0002");
    const segments = colored.split(/\u0001(kw|out|op|\/kw|\/out|\/op)\u0002/);
    let currentStyle: React.CSSProperties | null = null;
    const elements: React.ReactNode[] = [];

    segments.forEach((segment, segmentIndex) => {
      if (segment === "kw") {
        currentStyle = { color: t.acc, fontWeight: 700 };
        return;
      }
      if (segment === "out") {
        currentStyle = { color: t.ok, fontWeight: 700 };
        return;
      }
      if (segment === "op") {
        currentStyle = { color: t.warn };
        return;
      }
      if (segment.startsWith("/")) {
        currentStyle = null;
        return;
      }
      if (segment) {
        elements.push(
          <span key={segmentIndex} style={currentStyle ?? undefined}>
            {segment}
          </span>
        );
      }
    });

    return <div key={index}>{elements}</div>;
  });
}

function RulesList({
  rules,
  onLoad
}: {
  rules: RuleRecord[];
  onLoad: (rule: RuleRecord) => void;
}) {
  const { t } = useTheme();

  return (
    <div style={{ padding: 24, fontFamily: "'DM Sans',sans-serif", overflowY: "auto", flex: 1 }}>
      <h3 style={{ color: t.tx, fontSize: 16, fontWeight: 700, margin: "0 0 16px" }}>Saved Rules</h3>
      {!rules.length ? (
        <div style={{ color: t.tx3, fontSize: 13 }}>No rules saved yet.</div>
      ) : (
        rules.map((rule) => (
          <div
            key={rule.id}
            data-testid={`rules-list-item-${rule.id}`}
            onClick={() => onLoad(rule)}
            style={{
              padding: "12px 14px",
              background: t.bg3,
              borderRadius: 9,
              border: `1px solid ${t.border}`,
              marginBottom: 6,
              cursor: "pointer",
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center"
            }}
          >
            <div>
              <div style={{ fontSize: 13.5, fontWeight: 700, color: t.tx }}>{rule.name}</div>
              <div style={{ fontSize: 11.5, color: t.tx3 }}>
                {rule.definition.nodes.length} nodes · {rule.definition.connections.length} connections
              </div>
            </div>
            <div style={{ fontSize: 11.5, color: t.tx2 }}>{new Date(rule.updatedAt).toLocaleString()}</div>
          </div>
        ))
      )}
    </div>
  );
}

function Versions({ versions }: { versions: RuleVersionRecord[] }) {
  const { t } = useTheme();

  return (
    <div style={{ padding: 24, fontFamily: "'DM Sans',sans-serif", overflowY: "auto", flex: 1 }}>
      <h3 style={{ color: t.tx, fontSize: 16, fontWeight: 700, margin: "0 0 16px" }}>Version History</h3>
      {!versions.length ? (
        <div style={{ color: t.tx3, fontSize: 13 }}>No versions yet. Save a rule to create a snapshot.</div>
      ) : (
        versions.map((version) => (
          <div
            key={version.id}
            data-testid={`version-row-${version.version}`}
            style={{
              padding: "11px 14px",
              background: t.bg3,
              borderRadius: 9,
              border: `1px solid ${t.border}`,
              marginBottom: 6,
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center"
            }}
          >
            <div>
              <div style={{ fontSize: 13, fontWeight: 700, color: t.tx }}>v{version.version}</div>
              <div style={{ fontSize: 11.5, color: t.tx3 }}>
                {version.definition.nodes.length} nodes · {version.definition.connections.length} connections
              </div>
            </div>
            <div style={{ fontSize: 11.5, color: t.tx2 }}>{new Date(version.createdAt).toLocaleString()}</div>
          </div>
        ))
      )}
    </div>
  );
}

function TestConsole({
  nodes,
  connections,
  definition,
  currentRuleId,
  onEvaluate,
  requireSavedRuleForEvaluation
}: {
  nodes: RuleNode[];
  connections: RuleConnection[];
  definition: RuleDefinition;
  currentRuleId?: string;
  onEvaluate?: RuleBuilderProps["onEvaluate"];
  requireSavedRuleForEvaluation?: boolean;
}) {
  const { t } = useTheme();
  const [inputs, setInputs] = useState<Record<string, string>>({});
  const [result, setResult] = useState<EvaluationResult | null>(null);
  const [jsonMode, setJsonMode] = useState(false);
  const [jsonText, setJsonText] = useState("{}");

  const fields = useMemo(
    () =>
      [...new Set(
        nodes
          .filter((node) => node.type === "condition" || node.type === "score")
          .map((node) => ({ name: node.config?.field, type: node.config?.fieldType }))
          .filter((field) => field.name)
          .map((field) => JSON.stringify(field))
      )].map((field) => JSON.parse(field) as { name: string; type?: FieldType }),
    [nodes]
  );

  const run = async () => {
    if (requireSavedRuleForEvaluation && !currentRuleId && onEvaluate) {
      return;
    }

    let data: Record<string, unknown> = inputs;

    if (jsonMode) {
      try {
        data = JSON.parse(jsonText) as Record<string, unknown>;
      } catch {
        return;
      }
    }

    if (onEvaluate) {
      setResult(await onEvaluate({ ruleId: currentRuleId, definition, input: data }));
      return;
    }

    setResult(prototypeResultToEvaluation(evaluatePrototypeRule(nodes, connections, data), nodes));
  };

  const runDisabled = (fields.length === 0 && !jsonMode) || (Boolean(requireSavedRuleForEvaluation && !currentRuleId && onEvaluate));

  return (
    <div style={{ padding: 24, maxWidth: 680, fontFamily: "'DM Sans',sans-serif", overflowY: "auto", flex: 1 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
        <h3 style={{ color: t.tx, fontSize: 16, fontWeight: 700, margin: 0 }}>Test Console</h3>
        <button data-testid="test-console-mode-toggle" onClick={() => setJsonMode((value) => !value)} style={{ ...S.btn(t, false), fontSize: 12 }}>
          {jsonMode ? "Form Mode" : "JSON Mode"}
        </button>
      </div>

      {requireSavedRuleForEvaluation && !currentRuleId && onEvaluate ? (
        <div style={{ color: t.tx3, fontSize: 13, marginBottom: 14 }}>Save the rule before running server-side tests.</div>
      ) : null}

      {fields.length === 0 && !jsonMode ? (
        <div style={{ color: t.tx3, fontSize: 13 }}>Add conditions with field names in the Visual Builder first.</div>
      ) : jsonMode ? (
        <div style={{ marginBottom: 16 }}>
          <label style={S.label(t)}>Input JSON</label>
          <textarea
            data-testid="test-console-json"
            value={jsonText}
            onChange={(event) => setJsonText(event.target.value)}
            rows={8}
            style={{ ...S.input(t), resize: "vertical", fontFamily: "'JetBrains Mono',monospace", fontSize: 12 }}
          />
        </div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 10, marginBottom: 16 }}>
          {fields.map((field) => (
            <div key={field.name}>
              <label style={S.label(t)}>
                {field.name} <span style={{ fontWeight: 400, textTransform: "none" }}>({field.type})</span>
              </label>
              {field.type === "boolean" ? (
                <select
                  data-testid={`test-field-${field.name}`}
                  value={inputs[field.name] || ""}
                  onChange={(event) => setInputs((current) => ({ ...current, [field.name]: event.target.value }))}
                  style={S.select(t)}
                >
                  <option value="">— select —</option>
                  <option value="true">true</option>
                  <option value="false">false</option>
                </select>
              ) : (
                <input
                  data-testid={`test-field-${field.name}`}
                  value={inputs[field.name] || ""}
                  onChange={(event) => setInputs((current) => ({ ...current, [field.name]: event.target.value }))}
                  type={fieldInputType(field.type)}
                  placeholder={`Enter ${field.name}`}
                  style={S.input(t)}
                />
              )}
            </div>
          ))}
        </div>
      )}

      <button
        data-testid="test-console-run"
        onClick={() => void run()}
        disabled={runDisabled}
        style={{ ...S.btn(t, true), opacity: runDisabled ? 0.4 : 1, cursor: runDisabled ? "not-allowed" : "pointer" }}
      >
        Run Test
      </button>

      {result ? (
        <div style={{ marginTop: 20 }}>
          <div
            data-testid="test-console-result-banner"
            style={{
              padding: 14,
              borderRadius: 9,
              background: result.passed ? t.okBg : t.errBg,
              border: `1px solid ${(result.passed ? t.ok : t.err)}22`
            }}
          >
            <span style={{ fontSize: 14, fontWeight: 800, color: result.passed ? t.ok : t.err }}>
              ⇒ {result.outcome.toUpperCase()}
            </span>
          </div>
          <div style={{ marginTop: 14, display: "flex", flexDirection: "column", gap: 5 }}>
            {Object.entries(result.conditionResults).map(([id, condition]) => {
              const node = nodes.find((item) => item.id === id);
              return (
                <div
                  key={id}
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    alignItems: "center",
                    padding: "7px 12px",
                    background: t.bg3,
                    borderRadius: 7,
                    border: `1px solid ${t.border}`
                  }}
                >
                  <span style={{ fontSize: 12.5, color: t.tx }}>{node?.config?.field || node?.label}</span>
                  <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                    {condition.actual !== undefined ? <span style={{ fontSize: 11, color: t.tx3 }}>got: {String(condition.actual)}</span> : null}
                    {condition.reason ? <span style={{ fontSize: 11, color: t.tx3 }}>{condition.reason}</span> : null}
                    <span style={{ fontSize: 11, fontWeight: 700, color: condition.pass ? t.ok : t.err }}>
                      {condition.pass ? "PASS" : "FAIL"}
                    </span>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      ) : null}
    </div>
  );
}

export function RuleBuilder(props: RuleBuilderProps) {
  return (
    <ThemeProvider initialMode={props.theme ?? "dark"}>
      <RuleBuilderContent {...props} />
    </ThemeProvider>
  );
}

function RuleBuilderContent({
  environment = "prod",
  onEnvironmentChange,
  initialRule,
  theme,
  readOnly,
  requireSavedRuleForEvaluation,
  onSave,
  onLoad,
  onListRules,
  onLoadVersions,
  onEvaluate,
  onValidate
}: RuleBuilderProps) {
  const { mode, setMode, t } = useTheme();
  const [tab, setTab] = useState<"rules" | "visual" | "test" | "history">("visual");
  const [nodes, setNodes, undoNodes] = useUndoState<RuleNode[]>(initialRule?.nodes ?? []);
  const [connections, setConnections, undoConnections] = useUndoState<RuleConnection[]>(initialRule?.connections ?? []);
  const [rules, setRules] = useState<RuleRecord[]>([]);
  const [versions, setVersions] = useState<RuleVersionRecord[]>([]);
  const [saveModal, setSaveModal] = useState(false);
  const [ruleName, setRuleName] = useState(initialRule?.metadata?.name ?? "");
  const [env, setEnv] = useState<EnvironmentName>(environment);
  const [currentRuleId, setCurrentRuleId] = useState<string | undefined>(undefined);
  const [issues, setIssues] = useState<ValidationIssue[]>([]);
  const didLoadRef = useRef(false);

  useEffect(() => {
    ensurePrototypeFonts();
  }, []);

  useEffect(() => {
    setMode(theme ?? "dark");
  }, [setMode, theme]);

  useEffect(() => {
    setEnv(environment);
  }, [environment]);

  const definition = useMemo<RuleDefinition>(
    () => ({
      nodes,
      connections,
      metadata: {
        name: ruleName,
        environment: env
      }
    }),
    [connections, env, nodes, ruleName]
  );

  const expression = useMemo(() => generatePrototypeExpression(nodes, connections), [connections, nodes]);

  useEffect(() => {
    const handleKey = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "z") {
        event.preventDefault();
        undoNodes();
        undoConnections();
      }
    };

    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, [undoConnections, undoNodes]);

  useEffect(() => {
    if (!onListRules) {
      return;
    }

    void onListRules(env).then(setRules);
  }, [env, onListRules]);

  useEffect(() => {
    if (!onLoad || didLoadRef.current) {
      return;
    }

    didLoadRef.current = true;
    const mountPayload: RuleDefinition & { id?: string; name?: string; expression?: string } = {
      nodes: cloneDefinition(definition).nodes,
      connections: cloneDefinition(definition).connections,
      metadata: definition.metadata,
      id: currentRuleId,
      name: ruleName,
      expression
    };

    void Promise.resolve(onLoad(mountPayload)).then((payload) => {
      if (!payload) {
        return;
      }

      const normalized = normalizeLoadedRule(payload);
      setCurrentRuleId(normalized.id);
      setRuleName(normalized.name);
      setNodes(normalized.definition.nodes);
      setConnections(normalized.definition.connections);
    });
  }, [currentRuleId, definition, expression, onLoad, ruleName, setConnections, setNodes]);

  useEffect(() => {
    if (!currentRuleId || !onLoadVersions) {
      return;
    }

    void onLoadVersions(currentRuleId).then(setVersions);
  }, [currentRuleId, onLoadVersions]);

  useDebouncedEffect(
    async () => {
      if (onValidate) {
        const result = await onValidate(definition);
        setIssues(result.issues);
        return;
      }

      setIssues(validatePrototypeRule(nodes, connections));
    },
    [connections, definition, nodes, onValidate],
    200
  );

  const visibleRules = onListRules ? rules : rules.filter((rule) => rule.environment === env);
  const visibleVersions = onLoadVersions ? versions : versions.filter((version) => version.ruleId === currentRuleId);

  const toggleTheme = () => setMode((value) => (value === "dark" ? "light" : "dark"));

  const switchEnvironment = (nextEnvironment: EnvironmentName) => {
    setEnv(nextEnvironment);
    onEnvironmentChange?.(nextEnvironment);
  };

  const loadRule = (rule: RuleRecord) => {
    setCurrentRuleId(rule.id);
    setRuleName(rule.name);
    setNodes(cloneDefinition(rule.definition).nodes);
    setConnections(cloneDefinition(rule.definition).connections);
    setTab("visual");

    if (!onLoadVersions) {
      setVersions((current) => current);
    }
  };

  const runSave = async () => {
    const name = ruleName || `Rule ${visibleRules.length + 1}`;

    if (onSave) {
      const result = await onSave({
        id: currentRuleId,
        name,
        environment: env,
        definition,
        tags: definition.metadata?.tags ?? [],
        nodes,
        connections,
        expression
      });

      if (result?.id) {
        setCurrentRuleId(result.id);
      }

      if (onListRules) {
        setRules(await onListRules(env));
      }

      if (result?.id && onLoadVersions) {
        setVersions(await onLoadVersions(result.id));
      }

      setSaveModal(false);
      return;
    }

    const existingRule = currentRuleId ? rules.find((rule) => rule.id === currentRuleId) : undefined;
    const nextVersion = (existingRule?.currentVersion ?? 0) + 1;
    const nextRuleId = existingRule?.id ?? `local_rule_${Date.now().toString(36)}`;
    const ruleRecord = createLocalRuleRecord({
      id: nextRuleId,
      name,
      environment: env,
      expression,
      definition,
      currentVersion: nextVersion
    });
    const versionRecord = createLocalVersionRecord({
      ruleId: nextRuleId,
      version: nextVersion,
      environment: env,
      expression,
      definition
    });

    setCurrentRuleId(nextRuleId);
    setRules((current) => {
      const nextRules = current.filter((rule) => rule.id !== nextRuleId);
      return [ruleRecord, ...nextRules];
    });
    setVersions((current) => [versionRecord, ...current]);
    setSaveModal(false);
  };

  const exportJSON = () => {
    const blob = new Blob(
      [
        JSON.stringify(
          {
            nodes,
            connections,
            expression,
            exportedAt: new Date().toISOString()
          },
          null,
          2
        )
      ],
      { type: "application/json" }
    );
    const anchor = document.createElement("a");
    anchor.href = URL.createObjectURL(blob);
    anchor.download = `rule_${Date.now()}.json`;
    anchor.click();
  };

  const importJSON = () => {
    if (readOnly) {
      return;
    }

    const input = document.createElement("input");
    input.type = "file";
    input.accept = ".json";
    input.onchange = (event) => {
      const file = (event.target as HTMLInputElement).files?.[0];
      if (!file) {
        return;
      }

      const reader = new FileReader();
      reader.onload = (loadEvent) => {
        try {
          const data = JSON.parse(String(loadEvent.target?.result || "{}")) as {
            nodes?: RuleNode[];
            connections?: RuleConnection[];
          };
          setNodes(Array.isArray(data.nodes) ? data.nodes : []);
          setConnections(Array.isArray(data.connections) ? data.connections : []);
        } catch {
          // Prototype behavior: silent no-op on malformed JSON.
        }
      };
      reader.readAsText(file);
    };
    input.click();
  };

  const resetCanvas = () => {
    if (readOnly) {
      return;
    }

    setNodes([]);
    setConnections([]);
    setCurrentRuleId(undefined);
    setRuleName("");
  };

  return (
    <div
      data-testid="rule-builder-root"
      style={{
        width: "100%",
        height: "100%",
        minHeight: "calc(100vh - 120px)",
        background: t.bg,
        color: t.tx,
        fontFamily: "'DM Sans',sans-serif",
        display: "flex",
        flexDirection: "column",
        overflow: "hidden"
      }}
    >
      <div
        data-testid="builder-header"
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          padding: "9px 18px",
          borderBottom: `1px solid ${t.border}`,
          background: t.bg2,
          flexShrink: 0
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <span style={{ fontSize: 15, fontWeight: 800, letterSpacing: "-.02em" }}>Rule Builder</span>
          <span style={{ fontSize: 11, color: t.tx3 }}>Open-Source Visual Rule Engine</span>
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <button
            data-testid="theme-toggle"
            onClick={toggleTheme}
            style={{
              background: "none",
              border: `1px solid ${t.border}`,
              borderRadius: 7,
              padding: "5px 10px",
              cursor: "pointer",
              color: t.tx2,
              fontSize: 12,
              display: "flex",
              alignItems: "center",
              gap: 5,
              fontFamily: "'DM Sans',sans-serif"
            }}
          >
            {mode === "dark" ? "☀ Light" : "☾ Dark"}
          </button>
          <div style={{ display: "flex", gap: 3 }}>
            {[
              ["prod", "Prod"],
              ["uat", "UAT"],
              ["dev", "Dev"]
            ].map(([id, label]) => (
              <button
                key={id}
                data-testid={`env-pill-${id}`}
                onClick={() => switchEnvironment(id as EnvironmentName)}
                style={{
                  fontSize: 11,
                  padding: "4px 10px",
                  borderRadius: 6,
                  border: "none",
                  cursor: "pointer",
                  background: env === id ? t.acc : t.bg3,
                  color: env === id ? "#fff" : t.tx3,
                  fontWeight: 600,
                  fontFamily: "'DM Sans',sans-serif"
                }}
              >
                {label}
              </button>
            ))}
          </div>
        </div>
      </div>

      <div style={{ display: "flex", borderBottom: `1px solid ${t.border}`, paddingLeft: 14, flexShrink: 0 }}>
        {[
          ["rules", "Rules"],
          ["visual", "Visual Builder"],
          ["test", "Test Console"],
          ["history", "Version History"]
        ].map(([id, label]) => (
          <button
            key={id}
            data-testid={`tab-${id}`}
            onClick={() => setTab(id as typeof tab)}
            style={{
              background: "none",
              border: "none",
              padding: "11px 18px",
              cursor: "pointer",
              fontSize: 13,
              fontWeight: 600,
              color: tab === id ? t.tx : t.tx3,
              borderBottom: tab === id ? `2px solid ${t.acc}` : "2px solid transparent",
              fontFamily: "'DM Sans',sans-serif"
            }}
          >
            {label}
          </button>
        ))}
      </div>

      <div style={{ flex: 1, display: "flex", flexDirection: "column", overflow: "hidden", minHeight: 0 }}>
        {tab === "rules" ? <RulesList rules={visibleRules} onLoad={loadRule} /> : null}

        {tab === "visual" ? (
          <div style={{ flex: 1, display: "flex", flexDirection: "column", overflow: "hidden", minHeight: 0 }}>
            <div
              style={{
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
                padding: "8px 14px",
                borderBottom: `1px solid ${t.border}`,
                background: t.bg2,
                gap: 8,
                flexShrink: 0
              }}
            >
              <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                <span style={{ fontSize: 13.5, fontWeight: 700 }}>Visual Builder</span>
                <span style={{ fontSize: 11, color: t.tx3 }}>({nodes.length} nodes · {connections.length} connections)</span>
              </div>
              <div style={{ display: "flex", gap: 6 }}>
                <Button data-testid="visual-import" disabled={readOnly} onClick={importJSON}>
                  Import
                </Button>
                <Button data-testid="visual-export" onClick={exportJSON}>
                  Export
                </Button>
                <Button data-testid="visual-clear" disabled={readOnly} onClick={resetCanvas}>
                  Clear
                </Button>
                <Button data-testid="visual-save" primary disabled={readOnly} onClick={() => setSaveModal(true)}>
                  Save
                </Button>
              </div>
            </div>

            {issues.length > 0 ? (
              <div
                data-testid="validation-bar"
                style={{
                  padding: "6px 14px",
                  borderBottom: `1px solid ${t.border}`,
                  display: "flex",
                  flexWrap: "wrap",
                  gap: 4,
                  flexShrink: 0
                }}
              >
                {issues.map((issue, index) => (
                  <span
                    key={`${issue.message}_${index}`}
                    data-testid={`validation-chip-${index}`}
                    style={{
                      fontSize: 11,
                      padding: "3px 8px",
                      borderRadius: 5,
                      background: issue.level === "error" ? t.errBg : t.warnBg,
                      color: issue.level === "error" ? t.err : t.warn
                    }}
                  >
                    {issue.level === "error" ? "✕" : "⚠"} {issue.message}
                  </span>
                ))}
              </div>
            ) : null}

            <div style={{ flex: 1, minHeight: 0 }}>
              <Canvas nodes={nodes} setNodes={setNodes} connections={connections} setConnections={setConnections} readOnly={readOnly} />
            </div>

            <div style={{ borderTop: `1px solid ${t.border}`, padding: "12px 14px", maxHeight: 180, overflow: "auto", flexShrink: 0 }}>
              <div
                style={{
                  fontSize: 10,
                  fontWeight: 700,
                  color: t.tx3,
                  textTransform: "uppercase",
                  letterSpacing: ".06em",
                  marginBottom: 6
                }}
              >
                GENERATED EXPRESSION
              </div>
              <pre
                data-testid="expression-panel"
                style={{
                  background: t.code,
                  padding: 14,
                  borderRadius: 9,
                  fontSize: 12.5,
                  lineHeight: 1.65,
                  color: t.tx,
                  fontFamily: "'JetBrains Mono',monospace",
                  border: `1px solid ${t.border}`,
                  overflow: "auto",
                  margin: 0,
                  whiteSpace: "pre-wrap",
                  wordBreak: "break-word"
                }}
              >
                {highlightExpression(expression)}
              </pre>
            </div>
          </div>
        ) : null}

        {tab === "test" ? (
          <TestConsole
            nodes={nodes}
            connections={connections}
            definition={definition}
            currentRuleId={currentRuleId}
            onEvaluate={onEvaluate}
            requireSavedRuleForEvaluation={requireSavedRuleForEvaluation}
          />
        ) : null}

        {tab === "history" ? <Versions versions={visibleVersions} /> : null}
      </div>

      {saveModal ? (
        <div
          data-testid="save-modal-overlay"
          style={{
            position: "fixed",
            inset: 0,
            background: "rgba(0,0,0,.55)",
            zIndex: 200,
            display: "flex",
            alignItems: "center",
            justifyContent: "center"
          }}
          onClick={() => setSaveModal(false)}
        >
          <div
            data-testid="save-modal"
            style={{
              background: t.bgEl,
              borderRadius: 14,
              padding: 22,
              width: 360,
              border: `1px solid ${t.border}`,
              boxShadow: t.sh
            }}
            onClick={(event) => event.stopPropagation()}
          >
            <h3 style={{ color: t.tx, fontSize: 15, fontWeight: 700, margin: "0 0 14px" }}>Save Rule</h3>
            <label style={S.label(t)}>Rule Name</label>
            <input
              data-testid="save-rule-name"
              value={ruleName}
              onChange={(event) => setRuleName(event.target.value)}
              placeholder="Enter rule name"
              autoFocus
              style={{ ...S.input(t), marginBottom: 16 }}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  void runSave();
                }
              }}
            />
            <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
              <Button onClick={() => setSaveModal(false)}>Cancel</Button>
              <Button primary onClick={() => void runSave()}>
                Save
              </Button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
