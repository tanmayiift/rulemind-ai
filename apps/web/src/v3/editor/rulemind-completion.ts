/**
 * RuleMind-aware completion for the Python variable editor.
 *
 * Kept free of every CodeMirror import on purpose: the interesting behaviour is "which
 * completions does this cursor position deserve", and that is worth testing without
 * standing up a DOM, a view and a document. `python-editor-extensions.ts` is the thin
 * adapter that turns a `RuleMindCompletionResult` into a CodeMirror `CompletionResult`.
 *
 * The insertions are shaped by what the sandbox actually passes to a variable function
 * (apps/python-executor/app/sandbox.py: `function(payload, variables_map, {"connectors": {}})`).
 * All three are plain dicts, so `payload.summary` would raise AttributeError at run time.
 * Typing `payload.` is still the natural way to ask "what is in here", so a dot completion
 * is offered and then rewrites the dot into a subscript — the user types `payload.` and
 * gets `payload["summary"]`, which is the access style every seeded variable uses.
 */

export interface RuleMindVariableSummary {
  id: string;
  name: string;
  category?: string;
  description?: string | null;
}

export interface RuleMindCompletionScope {
  /** `sample_payload` of the connector currently selected on the page. */
  samplePayload?: Record<string, unknown> | null;
  /** Every variable the page has loaded — the keys of the `variables` dict at run time. */
  variables?: RuleMindVariableSummary[];
  /** Connector currently selected, used for the `@variable(source=...)` snippet. */
  sourceId?: string;
  /** Every connector id, for completing the decorator's `source=` string. */
  sourceIds?: string[];
}

export interface RuleMindCompletionQuery {
  /** The current line up to the cursor. Everything this module decides is decided from it. */
  textBefore: string;
  /** Absolute document offset of the cursor, so returned offsets are document offsets. */
  pos: number;
  /** The user pressed the completion key rather than merely typing. */
  explicit?: boolean;
}

export type RuleMindCompletionKind = "property" | "variable" | "keyword" | "text";

export interface RuleMindCompletionOption {
  /** Shown in the popup and matched against what the user has typed. */
  label: string;
  /** Text written into the document. Differs from `label` for subscript rewrites. */
  insert: string;
  /**
   * Absolute offset where `insert` starts replacing. Normally equal to the result's `from`;
   * lower when the completion has to swallow something the user already typed, which is how
   * `payload.` becomes `payload["summary"]`.
   */
  insertFrom: number;
  kind: RuleMindCompletionKind;
  detail?: string;
  info?: string;
  boost?: number;
}

export interface RuleMindCompletionResult {
  /** Start of the text the popup filters on. */
  from: number;
  options: RuleMindCompletionOption[];
}

const PARAMETERS: Array<{ label: string; detail: string; info: string }> = [
  { label: "payload", detail: "parameter · dict", info: "The raw source payload for this variable's connector." },
  { label: "variables", detail: "parameter · dict", info: "Values of variables already computed in this decision, keyed by id." },
  { label: "apis", detail: "parameter · dict", info: 'Runtime API handles. The sandbox passes {"connectors": {}}.' },
];

/** Roots whose contents this module can describe, and where their shape comes from. */
type Root = "payload" | "variables" | "apis";

const ROOTS: Root[] = ["payload", "variables", "apis"];

/**
 * A path segment written either as `.key` or as `["key"]` / `['key']`. Both appear in real
 * variable code, so a completion that only understood one of them would stop working the
 * moment it inserted its own subscript.
 */
const SEGMENT = /^(?:\.([A-Za-z_]\w*)|\[\s*(["'])((?:(?!\2).)*)\2\s*\])/;

/** `payload["summary"].avg` → root `payload`, path `["summary"]`, trailing word `avg`. */
function parseAccess(text: string): { root: Root; path: string[]; word: string; dotAt: number } | null {
  const trailing = /\.(\w*)$/.exec(text);
  if (!trailing) {
    return null;
  }
  const dotAt = text.length - trailing[0].length;
  const chain = text.slice(0, dotAt);
  const rootMatch = /(?:^|[^\w.\]"'])(payload|variables|apis)((?:\.[A-Za-z_]\w*|\[\s*(["'])(?:(?!\3).)*\3\s*\])*)$/.exec(chain);
  if (!rootMatch) {
    return null;
  }
  const path = readSegments(rootMatch[2]);
  if (path === null) {
    return null;
  }
  return { root: rootMatch[1] as Root, path, word: trailing[1], dotAt };
}

/** `["summary"].totals` → `["summary", "totals"]`; null if anything in it is not a literal key. */
function readSegments(raw: string): string[] | null {
  const segments: string[] = [];
  let rest = raw;
  while (rest.length > 0) {
    const match = SEGMENT.exec(rest);
    if (!match) {
      return null;
    }
    segments.push(match[1] !== undefined ? match[1] : match[3]);
    rest = rest.slice(match[0].length);
  }
  return segments;
}

/** `payload["summary"]["av` → root `payload`, path `["summary"]`, partial key `av`. */
function parseBracketKey(text: string): { root: Root; path: string[]; word: string; from: number } | null {
  const open = /\[\s*(["'])((?:(?!\1).)*)$/.exec(text);
  if (!open) {
    return null;
  }
  const chain = text.slice(0, text.length - open[0].length);
  const rootMatch = /(?:^|[^\w.\]"'])(payload|variables|apis)((?:\.[A-Za-z_]\w*|\[\s*(["'])(?:(?!\3).)*\3\s*\])*)$/.exec(chain);
  if (!rootMatch) {
    return null;
  }
  const path = readSegments(rootMatch[2]);
  if (path === null) {
    return null;
  }
  return { root: rootMatch[1] as Root, path, word: open[2], from: text.length - open[2].length };
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function resolvePath(root: unknown, path: string[]): unknown {
  let current: unknown = root;
  for (const segment of path) {
    if (!isPlainObject(current)) {
      return undefined;
    }
    current = current[segment];
  }
  return current;
}

/**
 * The `detail` line beside a field in the popup. Python type names, not JavaScript ones —
 * the reader is writing Python, and `str`/`int`/`dict` is what the sandbox will report back
 * in an error. Containers show their size instead of a value, since the value is the point
 * of drilling further in.
 */
function describeValue(value: unknown): string {
  if (value === null) {
    return "None";
  }
  if (Array.isArray(value)) {
    return "list[" + value.length + "]";
  }
  if (isPlainObject(value)) {
    return "dict{" + Object.keys(value).length + "}";
  }
  if (typeof value === "string") {
    const shown = value.length > 24 ? value.slice(0, 24) + "…" : value;
    return "str · " + JSON.stringify(shown);
  }
  if (typeof value === "number") {
    return (Number.isInteger(value) ? "int · " : "float · ") + String(value);
  }
  if (typeof value === "boolean") {
    return "bool · " + String(value);
  }
  return typeof value;
}

/**
 * Where the line's `#` comment starts, or -1. A `#` inside a string literal is not one, which
 * is the only reason this needs a scan rather than an `indexOf`.
 */
function commentStart(text: string): number {
  let quote: string | null = null;
  for (let index = 0; index < text.length; index += 1) {
    const char = text[index];
    if (quote) {
      if (char === "\\") {
        index += 1;
      } else if (char === quote) {
        quote = null;
      }
      continue;
    }
    if (char === "#") {
      return index;
    }
    if (char === '"' || char === "'") {
      quote = char;
    }
  }
  return -1;
}

/**
 * True when the cursor sits inside an unterminated string literal. Callers check the two
 * string positions this module deliberately completes inside (a dict subscript and the
 * decorator's `source=`) first, so anything reaching here is prose.
 */
function inString(text: string): boolean {
  let quote: string | null = null;
  for (let index = 0; index < text.length; index += 1) {
    const char = text[index];
    if (quote) {
      if (char === "\\") {
        index += 1;
      } else if (char === quote) {
        quote = null;
      }
      continue;
    }
    if (char === '"' || char === "'") {
      quote = char;
    }
  }
  return quote !== null;
}

function memberOptions(
  root: Root,
  path: string[],
  scope: RuleMindCompletionScope,
  insertFrom: number,
  asSubscript: boolean
): RuleMindCompletionOption[] | null {
  const wrap = (key: string) => (asSubscript ? "[" + JSON.stringify(key) + "]" : key);

  if (root === "variables") {
    // The `variables` dict is keyed by variable id and its values are opaque results, so only
    // the top level can be described. Anything deeper is the caller's own data.
    if (path.length > 0) {
      return null;
    }
    return (scope.variables ?? []).map((variable) => ({
      label: variable.id,
      insert: wrap(variable.id),
      insertFrom,
      kind: "variable" as const,
      detail: variable.category ? "variable · " + variable.category : "variable",
      info: variable.description ? variable.name + " — " + variable.description : variable.name,
      boost: 1,
    }));
  }

  if (root === "apis") {
    if (path.length > 0) {
      return null;
    }
    return [
      {
        label: "connectors",
        insert: wrap("connectors"),
        insertFrom,
        kind: "property" as const,
        detail: "dict",
        info: "Connector handles injected by the runtime.",
      },
    ];
  }

  const target = resolvePath(scope.samplePayload ?? undefined, path);
  if (!isPlainObject(target)) {
    return null;
  }
  const prefix = "payload" + path.map((segment) => "[" + JSON.stringify(segment) + "]").join("");
  return Object.keys(target).map((key) => ({
    label: key,
    insert: wrap(key),
    insertFrom,
    kind: "property" as const,
    detail: describeValue(target[key]),
    info: prefix + "[" + JSON.stringify(key) + "]",
    boost: 2,
  }));
}

/**
 * The single decision this module exists to make. Returns null when nothing should be
 * offered — an unknown object, a scalar, prose inside a comment — rather than falling back
 * to a generic word list, because a popup that fires everywhere is worse than none.
 */
export function rulemindCompletions(
  query: RuleMindCompletionQuery,
  scope: RuleMindCompletionScope
): RuleMindCompletionResult | null {
  const text = query.textBefore;
  const lineStart = query.pos - text.length;
  const absolute = (offsetInLine: number) => lineStart + offsetInLine;

  if (commentStart(text) >= 0) {
    return null;
  }

  // `@variable(source="ban` — completed before the string check, since it IS inside a string.
  const sourceValue = /(?:^|[^\w])source\s*=\s*(["'])((?:(?!\1).)*)$/.exec(text);
  if (sourceValue && /@\s*variable\s*\(/.test(text)) {
    const typed = sourceValue[2];
    const from = absolute(text.length - typed.length);
    const ids = scope.sourceIds ?? [];
    return ids.length === 0
      ? null
      : {
          from,
          options: ids.map((id) => ({
            label: id,
            insert: id,
            insertFrom: from,
            kind: "text" as const,
            detail: "connector",
          })),
        };
  }

  const bracket = parseBracketKey(text);
  if (bracket) {
    const options = memberOptions(bracket.root, bracket.path, scope, absolute(bracket.from), false);
    return options && options.length > 0 ? { from: absolute(bracket.from), options } : null;
  }

  if (inString(text)) {
    return null;
  }

  const access = parseAccess(text);
  if (access) {
    // `insertFrom` is the dot, `from` is just past it: the popup filters on the word the user
    // typed while the accepted completion also eats the dot it replaces.
    const options = memberOptions(access.root, access.path, scope, absolute(access.dotAt), true);
    return options && options.length > 0 ? { from: absolute(access.dotAt + 1), options } : null;
  }

  // A dot that resolved to nothing must not fall through to the identifier list, or typing
  // `foo.` would helpfully suggest `payload`.
  if (/\.\w*$/.test(text)) {
    return null;
  }

  const word = /(?:^|[^\w.])(\w*)$/.exec(text);
  if (!word) {
    return null;
  }
  const typed = word[1];
  const decorator = /@\w*$/.test(text);
  if (typed.length === 0 && !query.explicit && !decorator) {
    return null;
  }
  const from = absolute(text.length - typed.length);
  const source = scope.sourceId ?? "custom";
  const options: RuleMindCompletionOption[] = decorator
    ? [
        {
          label: "variable",
          insert: 'variable(source="' + source + '")',
          insertFrom: from,
          kind: "keyword",
          detail: "decorator",
          info: "Binds this function to a connector; the sandbox reads it to pick the payload.",
        },
      ]
    : PARAMETERS.map((parameter) => ({
        label: parameter.label,
        insert: parameter.label,
        insertFrom: from,
        kind: "variable" as const,
        detail: parameter.detail,
        info: parameter.info,
      }));
  return { from, options };
}
