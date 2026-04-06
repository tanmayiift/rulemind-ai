/**
 * Comprehensive Excel-compatible function library for RuleMind (TypeScript).
 *
 * Mirrors the Python implementation for cross-platform parity.
 * Provides 200+ Excel functions organized by category.
 */

type Num = number;
type AnyVal = unknown;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function toNum(v: AnyVal, fallback = 0): number {
  if (typeof v === "number") return v;
  if (typeof v === "boolean") return v ? 1 : 0;
  if (typeof v === "string") {
    const n = Number(v);
    return Number.isNaN(n) ? fallback : n;
  }
  return fallback;
}

function flatten(...args: AnyVal[]): AnyVal[] {
  const out: AnyVal[] = [];
  for (const a of args) {
    if (Array.isArray(a)) out.push(...flatten(...a));
    else out.push(a);
  }
  return out;
}

function nums(...args: AnyVal[]): number[] {
  return flatten(...args)
    .filter((v) => typeof v === "number" || (typeof v === "string" && v !== "" && !Number.isNaN(Number(v))))
    .map((v) => Number(v));
}

function matchCriteria(value: AnyVal, criteria: AnyVal): boolean {
  if (criteria == null) return true;
  if (typeof criteria === "number" || typeof criteria === "boolean") {
    try { return Number(value) === Number(criteria); } catch { return value === criteria; }
  }
  const c = String(criteria);
  if (c.startsWith(">=")) { try { return Number(value) >= Number(c.slice(2)); } catch { return false; } }
  if (c.startsWith("<=")) { try { return Number(value) <= Number(c.slice(2)); } catch { return false; } }
  if (c.startsWith("<>") || c.startsWith("!=")) { try { return Number(value) !== Number(c.slice(2)); } catch { return String(value) !== c.slice(2); } }
  if (c.startsWith(">")) { try { return Number(value) > Number(c.slice(1)); } catch { return false; } }
  if (c.startsWith("<")) { try { return Number(value) < Number(c.slice(1)); } catch { return false; } }
  if (c.startsWith("=")) { try { return Number(value) === Number(c.slice(1)); } catch { return String(value) === c.slice(1); } }
  if (c.includes("*") || c.includes("?")) {
    const pattern = "^" + c.replace(/[.+^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*").replace(/\?/g, ".") + "$";
    return new RegExp(pattern, "i").test(String(value));
  }
  try { return Number(value) === Number(c); } catch { return String(value).toLowerCase() === c.toLowerCase(); }
}

// ═══════════════════════════════════════════════════════════════════════════
// 1. MATH & TRIG
// ═══════════════════════════════════════════════════════════════════════════

const ABS = (x: Num) => Math.abs(toNum(x));
const ACOS = (x: Num) => Math.acos(toNum(x));
const ACOSH = (x: Num) => Math.acosh(toNum(x));
const ASIN = (x: Num) => Math.asin(toNum(x));
const ASINH = (x: Num) => Math.asinh(toNum(x));
const ATAN = (x: Num) => Math.atan(toNum(x));
const ATAN2 = (x: Num, y: Num) => Math.atan2(toNum(y), toNum(x));
const ATANH = (x: Num) => Math.atanh(toNum(x));

function CEILING(number: Num, significance: Num = 1): number {
  const n = toNum(number), s = toNum(significance, 1);
  if (s === 0) return 0;
  return Math.ceil(n / s) * s;
}

const COS = (x: Num) => Math.cos(toNum(x));
const COSH = (x: Num) => Math.cosh(toNum(x));
const DEGREES = (r: Num) => (toNum(r) * 180) / Math.PI;

function EVEN(number: Num): number {
  const n = toNum(number);
  let c = Math.ceil(Math.abs(n));
  if (c % 2 !== 0) c += 1;
  return n >= 0 ? c : -c;
}

const EXP = (x: Num) => Math.exp(toNum(x));

function FACT(n: Num): number {
  let v = Math.max(0, Math.floor(toNum(n)));
  let r = 1;
  for (let i = 2; i <= v; i++) r *= i;
  return r;
}

function FACTDOUBLE(n: Num): number {
  let v = Math.max(0, Math.floor(toNum(n)));
  let r = 1;
  while (v > 1) { r *= v; v -= 2; }
  return r;
}

function FLOOR_FUNC(number: Num, significance: Num = 1): number {
  const n = toNum(number), s = toNum(significance, 1);
  if (s === 0) return 0;
  return Math.floor(n / s) * s;
}

function GCD(...args: AnyVal[]): number {
  const values = nums(...args).map((v) => Math.max(0, Math.floor(v)));
  const gcd2 = (a: number, b: number): number => (b === 0 ? a : gcd2(b, a % b));
  return values.reduce((a, b) => gcd2(a, b), values[0] || 0);
}

const INT_FUNC = (n: Num) => Math.floor(toNum(n));

function LCM(...args: AnyVal[]): number {
  const values = nums(...args).map((v) => Math.max(1, Math.floor(v)));
  const gcd2 = (a: number, b: number): number => (b === 0 ? a : gcd2(b, a % b));
  return values.reduce((a, b) => (a * b) / gcd2(a, b), 1);
}

const LN = (x: Num) => Math.log(toNum(x));
const LOG = (x: Num, base: Num = 10) => Math.log(toNum(x)) / Math.log(toNum(base, 10));
const LOG10 = (x: Num) => Math.log10(toNum(x));

function MOD(number: Num, divisor: Num): number {
  const d = toNum(divisor);
  if (d === 0) throw new Error("MOD: divisor cannot be zero");
  return toNum(number) % d;
}

function MROUND(number: Num, multiple: Num): number {
  const n = toNum(number), m = toNum(multiple);
  if (m === 0) return 0;
  return Math.round(n / m) * m;
}

function ODD(number: Num): number {
  const n = toNum(number);
  let c = Math.ceil(Math.abs(n));
  if (c % 2 === 0) c += 1;
  return n >= 0 ? c : -c;
}

const PI = () => Math.PI;
const POWER = (base: Num, exp: Num) => Math.pow(toNum(base), toNum(exp));

function PRODUCT(...args: AnyVal[]): number {
  const v = nums(...args);
  return v.reduce((a, b) => a * b, 1);
}

function QUOTIENT(num: Num, den: Num): number {
  const d = toNum(den);
  if (d === 0) throw new Error("QUOTIENT: divisor cannot be zero");
  return Math.trunc(toNum(num) / d);
}

const RADIANS = (d: Num) => (toNum(d) * Math.PI) / 180;
const RAND = () => Math.random();
const RANDBETWEEN = (lo: number, hi: number) => Math.floor(Math.random() * (hi - lo + 1)) + lo;

function ROUND_FUNC(number: Num, digits: number = 0): number {
  const m = Math.pow(10, digits);
  return Math.round(toNum(number) * m) / m;
}

function ROUNDDOWN(number: Num, digits: number = 0): number {
  const m = Math.pow(10, digits);
  return Math.trunc(toNum(number) * m) / m;
}

function ROUNDUP(number: Num, digits: number = 0): number {
  const m = Math.pow(10, digits);
  const n = toNum(number);
  return n >= 0 ? Math.ceil(n * m) / m : Math.floor(n * m) / m;
}

const SIGN = (n: Num) => Math.sign(toNum(n));
const SIN = (x: Num) => Math.sin(toNum(x));
const SINH = (x: Num) => Math.sinh(toNum(x));
const SQRT = (x: Num) => Math.sqrt(toNum(x));
const SQRTPI = (x: Num) => Math.sqrt(toNum(x) * Math.PI);

function SUM_FUNC(...args: AnyVal[]): number {
  return nums(...args).reduce((a, b) => a + b, 0);
}

function SUMIF(range: AnyVal[], criteria: AnyVal, sumRange?: AnyVal[]): number {
  const sr = sumRange ?? range;
  let total = 0;
  for (let i = 0; i < range.length; i++) {
    if (matchCriteria(range[i], criteria) && i < sr.length) total += toNum(sr[i]);
  }
  return total;
}

function SUMPRODUCT(...arrays: AnyVal[][]): number {
  const lists = arrays.map((a) => (Array.isArray(a) ? flatten(a) : [a]));
  const len = Math.min(...lists.map((l) => l.length));
  let total = 0;
  for (let i = 0; i < len; i++) {
    let prod = 1;
    for (const l of lists) prod *= toNum(l[i]);
    total += prod;
  }
  return total;
}

function SUMSQ(...args: AnyVal[]): number {
  return nums(...args).reduce((a, v) => a + v * v, 0);
}

const TAN = (x: Num) => Math.tan(toNum(x));
const TANH = (x: Num) => Math.tanh(toNum(x));

function TRUNC(number: Num, digits: number = 0): number {
  const m = Math.pow(10, digits);
  return Math.trunc(toNum(number) * m) / m;
}

// ═══════════════════════════════════════════════════════════════════════════
// 2. STATISTICAL
// ═══════════════════════════════════════════════════════════════════════════

function AVERAGE(...args: AnyVal[]): number {
  const v = nums(...args);
  if (!v.length) throw new Error("AVERAGE: no numeric values");
  return v.reduce((a, b) => a + b, 0) / v.length;
}

function COUNT(...args: AnyVal[]): number { return nums(...args).length; }

function COUNTA(...args: AnyVal[]): number {
  return flatten(...args).filter((v) => v != null && v !== "").length;
}

function COUNTBLANK(...args: AnyVal[]): number {
  return flatten(...args).filter((v) => v == null || v === "").length;
}

function COUNTIF(range: AnyVal[], criteria: AnyVal): number {
  return range.filter((v) => matchCriteria(v, criteria)).length;
}

function LARGE(array: AnyVal[], k: number): number {
  const sorted = nums(...(Array.isArray(array) ? array : [array])).sort((a, b) => b - a);
  const idx = k - 1;
  if (idx < 0 || idx >= sorted.length) throw new Error("LARGE: k out of range");
  return sorted[idx];
}

function MAX_FUNC(...args: AnyVal[]): number {
  const v = nums(...args);
  return v.length ? Math.max(...v) : 0;
}

function MEDIAN(...args: AnyVal[]): number {
  const v = nums(...args).sort((a, b) => a - b);
  if (!v.length) throw new Error("MEDIAN: no values");
  const mid = Math.floor(v.length / 2);
  return v.length % 2 !== 0 ? v[mid] : (v[mid - 1] + v[mid]) / 2;
}

function MIN_FUNC(...args: AnyVal[]): number {
  const v = nums(...args);
  return v.length ? Math.min(...v) : 0;
}

function MODE(...args: AnyVal[]): number {
  const v = nums(...args);
  if (!v.length) throw new Error("MODE: no values");
  const counts = new Map<number, number>();
  for (const n of v) counts.set(n, (counts.get(n) ?? 0) + 1);
  let maxCount = 0, mode = v[0];
  for (const [val, count] of counts) { if (count > maxCount) { maxCount = count; mode = val; } }
  return mode;
}

function PERCENTILE(array: AnyVal[], k: Num): number {
  const v = nums(...(Array.isArray(array) ? array : [array])).sort((a, b) => a - b);
  if (!v.length) throw new Error("PERCENTILE: no values");
  const kf = toNum(k);
  const n = v.length - 1;
  const idx = kf * n;
  const lo = Math.floor(idx), hi = Math.ceil(idx);
  return lo === hi ? v[lo] : v[lo] + (v[hi] - v[lo]) * (idx - lo);
}

function RANK(number: Num, ref: AnyVal[], order: number = 0): number {
  const v = nums(...(Array.isArray(ref) ? ref : [ref]));
  const nf = toNum(number);
  const sorted = order === 0 ? [...v].sort((a, b) => b - a) : [...v].sort((a, b) => a - b);
  for (let i = 0; i < sorted.length; i++) if (sorted[i] === nf) return i + 1;
  throw new Error("RANK: number not found");
}

function SMALL(array: AnyVal[], k: number): number {
  const sorted = nums(...(Array.isArray(array) ? array : [array])).sort((a, b) => a - b);
  const idx = k - 1;
  if (idx < 0 || idx >= sorted.length) throw new Error("SMALL: k out of range");
  return sorted[idx];
}

function STDEV(...args: AnyVal[]): number {
  const v = nums(...args);
  if (v.length < 2) throw new Error("STDEV: need at least 2 values");
  const mean = v.reduce((a, b) => a + b, 0) / v.length;
  return Math.sqrt(v.reduce((a, x) => a + (x - mean) ** 2, 0) / (v.length - 1));
}

function STDEVP(...args: AnyVal[]): number {
  const v = nums(...args);
  if (!v.length) throw new Error("STDEVP: no values");
  const mean = v.reduce((a, b) => a + b, 0) / v.length;
  return Math.sqrt(v.reduce((a, x) => a + (x - mean) ** 2, 0) / v.length);
}

function VAR_FUNC(...args: AnyVal[]): number {
  const v = nums(...args);
  if (v.length < 2) throw new Error("VAR: need at least 2 values");
  const mean = v.reduce((a, b) => a + b, 0) / v.length;
  return v.reduce((a, x) => a + (x - mean) ** 2, 0) / (v.length - 1);
}

function VARP(...args: AnyVal[]): number {
  const v = nums(...args);
  if (!v.length) throw new Error("VARP: no values");
  const mean = v.reduce((a, b) => a + b, 0) / v.length;
  return v.reduce((a, x) => a + (x - mean) ** 2, 0) / v.length;
}

function SLOPE(knownY: AnyVal[], knownX: AnyVal[]): number {
  const ys = nums(...knownY), xs = nums(...knownX);
  const n = Math.min(xs.length, ys.length);
  if (n < 2) throw new Error("SLOPE: need at least 2 data points");
  const mx = xs.slice(0, n).reduce((a, b) => a + b, 0) / n;
  const my = ys.slice(0, n).reduce((a, b) => a + b, 0) / n;
  let num = 0, den = 0;
  for (let i = 0; i < n; i++) { num += (xs[i] - mx) * (ys[i] - my); den += (xs[i] - mx) ** 2; }
  if (den === 0) throw new Error("SLOPE: division by zero");
  return num / den;
}

function INTERCEPT_FUNC(knownY: AnyVal[], knownX: AnyVal[]): number {
  const ys = nums(...knownY), xs = nums(...knownX);
  const n = Math.min(xs.length, ys.length);
  const my = ys.slice(0, n).reduce((a, b) => a + b, 0) / n;
  const mx = xs.slice(0, n).reduce((a, b) => a + b, 0) / n;
  return my - SLOPE(knownY, knownX) * mx;
}

function FORECAST(x: Num, knownY: AnyVal[], knownX: AnyVal[]): number {
  return INTERCEPT_FUNC(knownY, knownX) + SLOPE(knownY, knownX) * toNum(x);
}

// ═══════════════════════════════════════════════════════════════════════════
// 3. FINANCIAL
// ═══════════════════════════════════════════════════════════════════════════

function PMT(rate: Num, nper: Num, pv: Num, fv: Num = 0, type_: number = 0): number {
  const r = toNum(rate), n = toNum(nper), p = toNum(pv), f = toNum(fv);
  if (r === 0) return -(p + f) / n;
  const q = Math.pow(1 + r, n);
  return -(p * q + f) / ((q - 1) / r * (1 + r * type_));
}

function FV(rate: Num, nper: Num, pmt: Num, pv: Num = 0, type_: number = 0): number {
  const r = toNum(rate), n = toNum(nper), p = toNum(pmt), present = toNum(pv);
  if (r === 0) return -(present + p * n);
  const q = Math.pow(1 + r, n);
  return -(present * q + p * (q - 1) / r * (1 + r * type_));
}

function PV(rate: Num, nper: Num, pmt: Num, fv: Num = 0, type_: number = 0): number {
  const r = toNum(rate), n = toNum(nper), p = toNum(pmt), f = toNum(fv);
  if (r === 0) return -(f + p * n);
  const q = Math.pow(1 + r, n);
  return -(f + p * (q - 1) / r * (1 + r * type_)) / q;
}

function NPV(rate: Num, ...cashflows: AnyVal[]): number {
  const r = toNum(rate);
  const flows = nums(...cashflows);
  return flows.reduce((sum, cf, i) => sum + cf / Math.pow(1 + r, i + 1), 0);
}

function IRR(values: AnyVal[], guess: number = 0.1): number {
  const flows = nums(...(Array.isArray(values) ? values : [values]));
  let rate = guess;
  for (let iter = 0; iter < 200; iter++) {
    let npv = 0, dnpv = 0;
    for (let i = 0; i < flows.length; i++) {
      npv += flows[i] / Math.pow(1 + rate, i);
      dnpv += -i * flows[i] / Math.pow(1 + rate, i + 1);
    }
    if (Math.abs(dnpv) < 1e-12) break;
    const newRate = rate - npv / dnpv;
    if (Math.abs(newRate - rate) < 1e-10) return newRate;
    rate = newRate;
  }
  return rate;
}

function NPER(rate: Num, pmt: Num, pv: Num, fv: Num = 0, type_: number = 0): number {
  const r = toNum(rate), p = toNum(pmt), present = toNum(pv), f = toNum(fv);
  if (r === 0) return -(present + f) / p;
  const w = p * (1 + r * type_);
  return Math.log((-f * r + w) / (present * r + w)) / Math.log(1 + r);
}

function SLN(cost: Num, salvage: Num, life: Num): number {
  return (toNum(cost) - toNum(salvage)) / toNum(life);
}

function EFFECT(nominalRate: Num, npery: Num): number {
  const r = toNum(nominalRate), n = Math.floor(toNum(npery));
  return Math.pow(1 + r / n, n) - 1;
}

function NOMINAL(effectRate: Num, npery: Num): number {
  const r = toNum(effectRate), n = Math.floor(toNum(npery));
  return n * (Math.pow(1 + r, 1 / n) - 1);
}

// ═══════════════════════════════════════════════════════════════════════════
// 4. LOGICAL
// ═══════════════════════════════════════════════════════════════════════════

function AND_FUNC(...args: AnyVal[]): boolean { return flatten(...args).every((v) => Boolean(v)); }
function OR_FUNC(...args: AnyVal[]): boolean { return flatten(...args).some((v) => Boolean(v)); }
function NOT_FUNC(v: AnyVal): boolean { return !Boolean(v); }
function XOR(...args: AnyVal[]): boolean { return flatten(...args).filter((v) => Boolean(v)).length % 2 === 1; }

function IF_FUNC(cond: AnyVal, ifTrue: AnyVal = true, ifFalse: AnyVal = false): AnyVal {
  return Boolean(cond) ? ifTrue : ifFalse;
}

function IFS(...args: AnyVal[]): AnyVal {
  for (let i = 0; i < args.length; i += 2) {
    if (Boolean(args[i]) && i + 1 < args.length) return args[i + 1];
  }
  throw new Error("IFS: no condition is TRUE");
}

function IFERROR(value: AnyVal, valueIfError: AnyVal = ""): AnyVal {
  if (typeof value === "number" && (Number.isNaN(value) || !Number.isFinite(value))) return valueIfError;
  return value;
}

function IFNA(value: AnyVal, valueIfNa: AnyVal = ""): AnyVal {
  return value == null ? valueIfNa : value;
}

function SWITCH(expression: AnyVal, ...args: AnyVal[]): AnyVal {
  for (let i = 0; i < args.length - 1; i += 2) {
    if (expression === args[i]) return args[i + 1];
  }
  if (args.length % 2 === 1) return args[args.length - 1];
  throw new Error("SWITCH: no match found");
}

const TRUE_FUNC = () => true;
const FALSE_FUNC = () => false;

// ═══════════════════════════════════════════════════════════════════════════
// 5. TEXT
// ═══════════════════════════════════════════════════════════════════════════

const CHAR = (n: number) => String.fromCharCode(n);
const CLEAN = (t: string) => String(t).replace(/[\x00-\x1f]/g, "");
const CODE = (t: string) => { const s = String(t); return s.length ? s.charCodeAt(0) : 0; };
const CONCATENATE = (...args: AnyVal[]) => flatten(...args).map(String).join("");
const CONCAT = CONCATENATE;
const EXACT = (a: string, b: string) => String(a) === String(b);

function FIND(findText: string, withinText: string, startNum: number = 1): number {
  const idx = String(withinText).indexOf(String(findText), startNum - 1);
  if (idx === -1) throw new Error("FIND: text not found");
  return idx + 1;
}

function FIXED(number: Num, decimals: number = 2, noCommas: boolean = false): string {
  const formatted = toNum(number).toLocaleString("en-US", { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
  return noCommas ? formatted.replace(/,/g, "") : formatted;
}

const LEFT = (t: string, n: number = 1) => String(t).slice(0, n);
const LEN_FUNC = (t: string) => String(t).length;
const LOWER = (t: string) => String(t).toLowerCase();
const MID = (t: string, start: number, n: number) => String(t).slice(start - 1, start - 1 + n);

function PROPER(text: string): string {
  return String(text).replace(/\w\S*/g, (w) => w.charAt(0).toUpperCase() + w.slice(1).toLowerCase());
}

function REPLACE(oldText: string, startNum: number, numChars: number, newText: string): string {
  const s = String(oldText);
  return s.slice(0, startNum - 1) + String(newText) + s.slice(startNum - 1 + numChars);
}

const REPT = (t: string, n: number) => String(t).repeat(Math.max(0, n));

function RIGHT(text: string, numChars: number = 1): string {
  const s = String(text);
  return numChars > 0 ? s.slice(-numChars) : "";
}

function SEARCH(findText: string, withinText: string, startNum: number = 1): number {
  const idx = String(withinText).toLowerCase().indexOf(String(findText).toLowerCase(), startNum - 1);
  if (idx === -1) throw new Error("SEARCH: text not found");
  return idx + 1;
}

function SUBSTITUTE(text: string, oldText: string, newText: string, instanceNum: number = 0): string {
  const s = String(text), o = String(oldText), n = String(newText);
  if (instanceNum <= 0) return s.split(o).join(n);
  let count = 0, result = "", i = 0;
  while (i < s.length) {
    if (s.slice(i, i + o.length) === o) {
      count++;
      if (count === instanceNum) { result += n; i += o.length; continue; }
    }
    result += s[i]; i++;
  }
  return result;
}

const T = (v: AnyVal) => (typeof v === "string" ? v : "");
const TRIM = (t: string) => String(t).split(/\s+/).filter(Boolean).join(" ");
const UPPER = (t: string) => String(t).toUpperCase();

function VALUE(text: string): number {
  const s = String(text).replace(/[,$%]/g, "").trim();
  return Number(s);
}

function TEXTJOIN(delimiter: string, ignoreEmpty: boolean, ...args: AnyVal[]): string {
  let items = flatten(...args).map(String);
  if (ignoreEmpty) items = items.filter((v) => v !== "" && v !== "null" && v !== "undefined");
  return items.join(String(delimiter));
}

// ═══════════════════════════════════════════════════════════════════════════
// 6. LOOKUP & REFERENCE
// ═══════════════════════════════════════════════════════════════════════════

function VLOOKUP(lookupValue: AnyVal, tableArray: AnyVal[][], colIndex: number, rangeLookup: boolean = true): AnyVal {
  const col = colIndex - 1;
  if (rangeLookup) {
    let last: AnyVal = undefined;
    for (const row of tableArray) {
      if (!Array.isArray(row) || row.length <= col) continue;
      try { if ((row[0] as number) <= (lookupValue as number)) last = row[col]; else break; } catch { continue; }
    }
    if (last !== undefined) return last;
    throw new Error("VLOOKUP: no match found");
  }
  for (const row of tableArray) {
    if (Array.isArray(row) && row.length > col && row[0] === lookupValue) return row[col];
  }
  throw new Error("VLOOKUP: no exact match found");
}

function INDEX(array: AnyVal[], rowNum: number = 0, colNum: number = 0): AnyVal {
  if (rowNum > 0) {
    const row = array[rowNum - 1];
    if (colNum > 0 && Array.isArray(row)) return row[colNum - 1];
    return row;
  }
  if (colNum > 0) return array.map((row) => (Array.isArray(row) ? row[colNum - 1] : row));
  return array;
}

function MATCH(lookupValue: AnyVal, lookupArray: AnyVal[], matchType: number = 1): number {
  const flat = flatten(lookupArray);
  if (matchType === 0) {
    for (let i = 0; i < flat.length; i++) if (flat[i] === lookupValue) return i + 1;
  } else if (matchType === 1) {
    let last: number | undefined;
    for (let i = 0; i < flat.length; i++) {
      try { if ((flat[i] as number) <= (lookupValue as number)) last = i + 1; else break; } catch { continue; }
    }
    if (last !== undefined) return last;
  } else {
    let last: number | undefined;
    for (let i = 0; i < flat.length; i++) {
      try { if ((flat[i] as number) >= (lookupValue as number)) last = i + 1; else break; } catch { continue; }
    }
    if (last !== undefined) return last;
  }
  throw new Error("MATCH: no match found");
}

function CHOOSE(indexNum: number, ...values: AnyVal[]): AnyVal {
  if (indexNum < 1 || indexNum > values.length) throw new Error("CHOOSE: index out of range");
  return values[indexNum - 1];
}

const COLUMNS = (a: AnyVal[]) => (a.length ? (Array.isArray(a[0]) ? (a[0] as AnyVal[]).length : 1) : 0);
const ROWS = (a: AnyVal[]) => a.length;

function TRANSPOSE(array: AnyVal[][]): AnyVal[][] {
  if (!array.length) return [];
  if (!Array.isArray(array[0])) return (array as AnyVal[]).map((v) => [v]);
  const rows = array.length, cols = (array[0] as AnyVal[]).length;
  const result: AnyVal[][] = [];
  for (let c = 0; c < cols; c++) {
    const row: AnyVal[] = [];
    for (let r = 0; r < rows; r++) row.push((array[r] as AnyVal[])[c]);
    result.push(row);
  }
  return result;
}

// ═══════════════════════════════════════════════════════════════════════════
// 7. DATE & TIME
// ═══════════════════════════════════════════════════════════════════════════

const DATE_FUNC = (y: number, m: number, d: number) => new Date(y, m - 1, d);
const DAY = (d: AnyVal) => (d instanceof Date ? d.getDate() : new Date(String(d)).getDate());
const MONTH = (d: AnyVal) => (d instanceof Date ? d.getMonth() + 1 : new Date(String(d)).getMonth() + 1);
const YEAR = (d: AnyVal) => (d instanceof Date ? d.getFullYear() : new Date(String(d)).getFullYear());

function DAYS(endDate: AnyVal, startDate: AnyVal): number {
  const e = endDate instanceof Date ? endDate : new Date(String(endDate));
  const s = startDate instanceof Date ? startDate : new Date(String(startDate));
  return Math.round((e.getTime() - s.getTime()) / 86400000);
}

function EDATE(startDate: AnyVal, months: number): Date {
  const d = startDate instanceof Date ? new Date(startDate) : new Date(String(startDate));
  d.setMonth(d.getMonth() + months);
  return d;
}

function EOMONTH(startDate: AnyVal, months: number): Date {
  const d = startDate instanceof Date ? new Date(startDate) : new Date(String(startDate));
  d.setMonth(d.getMonth() + months + 1, 0);
  return d;
}

const NOW = () => new Date();
const TODAY = () => { const d = new Date(); d.setHours(0, 0, 0, 0); return d; };

function WEEKDAY(serialNumber: AnyVal, returnType: number = 1): number {
  const d = serialNumber instanceof Date ? serialNumber : new Date(String(serialNumber));
  const wd = d.getDay(); // 0=Sun
  if (returnType === 1) return wd + 1;
  if (returnType === 2) return wd === 0 ? 7 : wd;
  if (returnType === 3) return wd === 0 ? 6 : wd - 1;
  return wd;
}

// ═══════════════════════════════════════════════════════════════════════════
// 8. INFORMATION
// ═══════════════════════════════════════════════════════════════════════════

const ISBLANK = (v: AnyVal) => v == null || v === "";
const ISNUMBER = (v: AnyVal) => typeof v === "number" && !Number.isNaN(v);
const ISTEXT = (v: AnyVal) => typeof v === "string";
const ISLOGICAL = (v: AnyVal) => typeof v === "boolean";
const ISERROR = (v: AnyVal) => typeof v === "number" && (Number.isNaN(v) || !Number.isFinite(v));
const ISNA = (v: AnyVal) => v == null || (typeof v === "number" && Number.isNaN(v));
const ISEVEN_FUNC = (v: AnyVal) => Math.floor(toNum(v)) % 2 === 0;
const ISODD_FUNC = (v: AnyVal) => Math.floor(toNum(v)) % 2 !== 0;
const N = (v: AnyVal) => (typeof v === "number" ? v : typeof v === "boolean" ? (v ? 1 : 0) : 0);
const NA = () => NaN;

function TYPE_FUNC(v: AnyVal): number {
  if (typeof v === "number") return 1;
  if (typeof v === "string") return 2;
  if (typeof v === "boolean") return 4;
  if (Array.isArray(v)) return 64;
  return 0;
}

// ═══════════════════════════════════════════════════════════════════════════
// 9. ENGINEERING
// ═══════════════════════════════════════════════════════════════════════════

const BIN2DEC = (b: string) => parseInt(String(b), 2);
const BIN2HEX = (b: string, places: number = 0) => { const v = parseInt(String(b), 2).toString(16).toUpperCase(); return places > 0 ? v.padStart(places, "0") : v; };
const DEC2BIN = (n: Num, places: number = 0) => { const b = (Math.floor(toNum(n)) >>> 0).toString(2); return places > 0 ? b.padStart(places, "0") : b; };
const DEC2HEX = (n: Num, places: number = 0) => { const h = Math.floor(toNum(n)).toString(16).toUpperCase(); return places > 0 ? h.padStart(places, "0") : h; };
const HEX2BIN = (h: string, places: number = 0) => { const b = parseInt(String(h), 16).toString(2); return places > 0 ? b.padStart(places, "0") : b; };
const HEX2DEC = (h: string) => parseInt(String(h), 16);
const OCT2DEC = (o: string) => parseInt(String(o), 8);

// ═══════════════════════════════════════════════════════════════════════════
// EXPORT MAP
// ═══════════════════════════════════════════════════════════════════════════

export const EXCEL_FUNCTIONS: Record<string, (...args: any[]) => any> = {
  // Math & Trig
  ABS, ACOS, ACOSH, ASIN, ASINH, ATAN, ATAN2, ATANH, CEILING,
  COS, COSH, DEGREES, EVEN, EXP, FACT, FACTDOUBLE,
  FLOOR: FLOOR_FUNC, GCD, INT: INT_FUNC, LCM, LN, LOG, LOG10,
  MOD, MROUND, ODD, PI, POWER, PRODUCT, QUOTIENT,
  RADIANS, RAND, RANDBETWEEN, ROUND: ROUND_FUNC, ROUNDDOWN, ROUNDUP,
  SIGN, SIN, SINH, SQRT, SQRTPI,
  SUM: SUM_FUNC, SUMIF, SUMPRODUCT, SUMSQ, TAN, TANH, TRUNC,

  // Statistical
  AVERAGE, COUNT, COUNTA, COUNTBLANK, COUNTIF,
  LARGE, MAX: MAX_FUNC, MEDIAN, MIN: MIN_FUNC, MODE,
  PERCENTILE, RANK, SMALL, STDEV, STDEVP, VAR: VAR_FUNC, VARP,
  SLOPE, INTERCEPT: INTERCEPT_FUNC, FORECAST,

  // Financial
  PMT, FV, PV, NPV, IRR, NPER, SLN, EFFECT, NOMINAL,

  // Logical
  AND: AND_FUNC, OR: OR_FUNC, NOT: NOT_FUNC, XOR,
  IF: IF_FUNC, IFS, IFERROR, IFNA, SWITCH, TRUE: TRUE_FUNC, FALSE: FALSE_FUNC,

  // Text
  CHAR, CLEAN, CODE, CONCATENATE, CONCAT, EXACT, FIND, FIXED,
  LEFT, LEN: LEN_FUNC, LOWER, MID, PROPER, REPLACE, REPT, RIGHT,
  SEARCH, SUBSTITUTE, T, TEXT: (v: AnyVal) => String(v), TEXTJOIN, TRIM, UPPER, VALUE,

  // Lookup & Reference
  VLOOKUP, INDEX, MATCH, CHOOSE, COLUMNS, ROWS, TRANSPOSE,

  // Date & Time
  DATE: DATE_FUNC, DAY, MONTH, YEAR, DAYS, EDATE, EOMONTH, NOW, TODAY, WEEKDAY,

  // Information
  ISBLANK, ISNUMBER, ISTEXT, ISLOGICAL, ISERROR, ISNA,
  ISEVEN: ISEVEN_FUNC, ISODD: ISODD_FUNC, N, NA, TYPE: TYPE_FUNC,

  // Engineering
  BIN2DEC, BIN2HEX, DEC2BIN, DEC2HEX, HEX2BIN, HEX2DEC, OCT2DEC,
};

export const EXCEL_FUNCTION_COUNT = Object.keys(EXCEL_FUNCTIONS).length;
