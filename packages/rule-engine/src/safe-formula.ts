/**
 * A whitelisting expression evaluator for scorecard and metric formulas.
 *
 * WHY THIS EXISTS. `decisioning.ts` evaluated an author-supplied `formula` string with
 * `new Function(...fnNames, ...ctxNames, "return (" + formula + ");")`. That is real JavaScript
 * eval. A policy author could write
 *
 *     formula: "((globalThis.__PWNED__ = 'arbitrary JS ran'), 1)"
 *
 * and it ran — verified, not theorised. This engine executes in the dashboard, so that is
 * arbitrary JS in the browser of anyone who opens or tests that scorecard: stored XSS in a
 * multi-tenant product whose authors are explicitly untrusted. It is the same hazard that was
 * removed from the Python engine (`app/safe_formula.py`), and the Kotlin and Dart SDKs have
 * since grown their own bounded evaluators. This is the TypeScript arm of that contract.
 *
 * The approach is a denylist's opposite. Rather than trying to forbid `constructor`,
 * `__proto__`, `globalThis` and every other reachable escape — a game you lose, because
 * `(()=>{}).constructor` alone reconstructs `Function` — the grammar below simply has no
 * production for member access, subscripting, calling the result of a call, `new`, assignment,
 * functions or comprehensions. Those escapes cannot be *spelled*, so there is nothing to filter.
 *
 * Semantics follow CPython, because `app/logic.py` is the declared source of truth for all five
 * engines and its formulas are Python expressions:
 *   - `/` is true division, `//` floors, `%` takes the sign of the divisor
 *   - `and`/`or` return an operand, not a boolean, and short-circuit
 *   - comparisons chain: `0 < x < 10`
 *   - `a if c else b` is the conditional form (NOT the JS `c ? a : b`, which Python rejects)
 *   - booleans are numeric: `true + 1 === 2`
 */

export class FormulaError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "FormulaError";
  }
}

/** Guardrails on the two ways a pure-arithmetic expression can still burn a tab. */
const MAX_POW_EXPONENT = 1000;
const MAX_STRING_REPEAT = 100_000;
const MAX_EXPRESSION_LENGTH = 4096;

type Tok =
  | { k: "num"; v: number }
  | { k: "str"; v: string }
  | { k: "name"; v: string }
  | { k: "op"; v: string }
  | { k: "end" };

const OPERATORS = [
  "**", "//", "==", "!=", "<=", ">=", "(", ")", ",", "+", "-", "*", "/", "%", "<", ">",
];

function tokenize(src: string): Tok[] {
  const out: Tok[] = [];
  let i = 0;
  while (i < src.length) {
    const c = src[i];
    if (c === " " || c === "\t" || c === "\n" || c === "\r") {
      i += 1;
      continue;
    }
    if (c === '"' || c === "'") {
      // No escape processing: a formula is a number-shaped expression, and supporting escapes
      // only widens the surface. A backslash is a literal backslash.
      const end = src.indexOf(c, i + 1);
      if (end === -1) throw new FormulaError("unterminated string literal");
      out.push({ k: "str", v: src.slice(i + 1, end) });
      i = end + 1;
      continue;
    }
    if (/[0-9]/.test(c) || (c === "." && /[0-9]/.test(src[i + 1] ?? ""))) {
      const m = /^[0-9]*\.?[0-9]+([eE][+-]?[0-9]+)?/.exec(src.slice(i));
      if (!m) throw new FormulaError("malformed number");
      out.push({ k: "num", v: Number(m[0]) });
      i += m[0].length;
      continue;
    }
    if (/[A-Za-z_]/.test(c)) {
      const m = /^[A-Za-z_][A-Za-z0-9_]*/.exec(src.slice(i))!;
      out.push({ k: "name", v: m[0] });
      i += m[0].length;
      continue;
    }
    const op = OPERATORS.find((o) => src.startsWith(o, i));
    if (!op) throw new FormulaError(`unexpected character ${JSON.stringify(c)}`);
    // `.` is deliberately absent from OPERATORS, so member access is a tokenizer error rather
    // than something the parser has to reject. Same for `[`, `]`, `{`, `}`, `=`, `;`, `?`, `:`.
    out.push({ k: "op", v: op });
    i += op.length;
  }
  out.push({ k: "end" });
  return out;
}

type Ctx = Record<string, unknown>;
type Fns = Record<string, (...args: never[]) => unknown>;

function truthy(v: unknown): boolean {
  if (v === null || v === undefined || v === false) return false;
  if (v === 0 || v === "") return false;
  if (Array.isArray(v)) return v.length > 0;
  return true;
}

function numeric(v: unknown): number {
  if (typeof v === "boolean") return v ? 1 : 0; // Python: bools are ints
  if (typeof v === "number") return v;
  if (typeof v === "string" && v.trim() !== "" && !Number.isNaN(Number(v))) return Number(v);
  throw new FormulaError(`not a number: ${JSON.stringify(v)}`);
}

class Parser {
  private pos = 0;
  constructor(private toks: Tok[], private ctx: Ctx, private fns: Fns) {}

  private peek(): Tok {
    return this.toks[this.pos];
  }
  private eatOp(v: string): boolean {
    const t = this.peek();
    if (t.k === "op" && t.v === v) {
      this.pos += 1;
      return true;
    }
    return false;
  }
  private eatName(v: string): boolean {
    const t = this.peek();
    if (t.k === "name" && t.v === v) {
      this.pos += 1;
      return true;
    }
    return false;
  }
  private expectOp(v: string): void {
    if (!this.eatOp(v)) throw new FormulaError(`expected ${v}`);
  }

  parse(): unknown {
    const value = this.ternary();
    if (this.peek().k !== "end") throw new FormulaError("trailing tokens after expression");
    return value;
  }

  /** Python's conditional: `a if cond else b`. Lowest precedence. */
  private ternary(): unknown {
    const body = this.orExpr();
    if (this.eatName("if")) {
      const cond = this.orExpr();
      if (!this.eatName("else")) throw new FormulaError("conditional requires `else`");
      const other = this.ternary();
      return truthy(cond) ? body : other;
    }
    return body;
  }

  private orExpr(): unknown {
    let left = this.andExpr();
    while (this.eatName("or")) {
      // Python `or` returns an OPERAND and short-circuits, so the right side must not be
      // evaluated when the left is truthy.
      if (truthy(left)) {
        this.skipOperand(() => this.andExpr());
        continue;
      }
      left = this.andExpr();
    }
    return left;
  }

  private andExpr(): unknown {
    let left = this.notExpr();
    while (this.eatName("and")) {
      if (!truthy(left)) {
        this.skipOperand(() => this.notExpr());
        continue;
      }
      left = this.notExpr();
    }
    return left;
  }

  /** Short-circuit still has to CONSUME the skipped operand; evaluating it is the only way to
   *  walk past it with this parser shape, so it is evaluated and discarded. Safe because every
   *  reachable production is side-effect free — there is no assignment or call-with-effects in
   *  the grammar. */
  private skipOperand(read: () => unknown): void {
    read();
  }

  private notExpr(): unknown {
    if (this.eatName("not")) return !truthy(this.notExpr());
    return this.comparison();
  }

  private comparison(): unknown {
    let left = this.additive();
    let result = true;
    let compared = false;
    for (;;) {
      let op: string | null = null;
      for (const candidate of ["==", "!=", "<=", ">=", "<", ">"]) {
        if (this.eatOp(candidate)) {
          op = candidate;
          break;
        }
      }
      if (op === null) break;
      const right = this.additive();
      compared = true;
      result = result && this.compareOne(left, op, right);
      left = right; // Python chains: 0 < x < 10
    }
    return compared ? result : left;
  }

  private compareOne(a: unknown, op: string, b: unknown): boolean {
    if (op === "==" || op === "!=") {
      const eq = typeof a === typeof b ? a === b : numericSafe(a) !== null && numericSafe(a) === numericSafe(b);
      return op === "==" ? eq : !eq;
    }
    const x = numeric(a);
    const y = numeric(b);
    switch (op) {
      case "<": return x < y;
      case "<=": return x <= y;
      case ">": return x > y;
      default: return x >= y;
    }
  }

  private additive(): unknown {
    let left = this.multiplicative();
    for (;;) {
      if (this.eatOp("+")) {
        const r = this.multiplicative();
        left =
          typeof left === "string" && typeof r === "string"
            ? left + r
            : numeric(left) + numeric(r);
      } else if (this.eatOp("-")) {
        left = numeric(left) - numeric(this.multiplicative());
      } else break;
    }
    return left;
  }

  private multiplicative(): unknown {
    let left = this.unary();
    for (;;) {
      if (this.eatOp("*")) {
        const r = this.unary();
        if (typeof left === "string" || typeof r === "string") {
          const s = (typeof left === "string" ? left : r) as string;
          const n = numeric(typeof left === "string" ? r : left);
          if (n > MAX_STRING_REPEAT) throw new FormulaError("string repetition too large");
          left = s.repeat(Math.max(0, Math.trunc(n)));
        } else left = numeric(left) * numeric(r);
      } else if (this.eatOp("//")) {
        const d = numeric(this.unary());
        if (d === 0) throw new FormulaError("floordiv failed: division by zero");
        left = Math.floor(numeric(left) / d); // Python floors toward -inf
      } else if (this.eatOp("/")) {
        const d = numeric(this.unary());
        if (d === 0) throw new FormulaError("div failed: division by zero");
        left = numeric(left) / d;
      } else if (this.eatOp("%")) {
        const d = numeric(this.unary());
        if (d === 0) throw new FormulaError("mod failed: division by zero");
        const n = numeric(left);
        left = ((n % d) + d) % d; // Python's % takes the sign of the divisor
      } else break;
    }
    return left;
  }

  private unary(): unknown {
    if (this.eatOp("-")) return -numeric(this.unary());
    if (this.eatOp("+")) return numeric(this.unary());
    return this.power();
  }

  private power(): unknown {
    const base = this.atom();
    if (this.eatOp("**")) {
      const exp = numeric(this.unary()); // right-associative
      if (exp > MAX_POW_EXPONENT) throw new FormulaError("exponent too large");
      return Math.pow(numeric(base), exp);
    }
    return base;
  }

  private atom(): unknown {
    const t = this.peek();
    if (t.k === "num") {
      this.pos += 1;
      return t.v;
    }
    if (t.k === "str") {
      this.pos += 1;
      return t.v;
    }
    if (t.k === "op" && t.v === "(") {
      this.pos += 1;
      const inner = this.ternary();
      this.expectOp(")");
      // Deliberately NOT followed by a call check: `(expr)(args)` must not be reachable, or a
      // value that happens to be callable becomes an escape.
      return inner;
    }
    if (t.k === "name") {
      this.pos += 1;
      const name = t.v;
      if (name === "True") return true;
      if (name === "False") return false;
      if (name === "None") return null;
      if (this.eatOp("(")) {
        const fn = this.fns[name];
        if (typeof fn !== "function") throw new FormulaError(`unknown function: ${name}`);
        const args: unknown[] = [];
        if (!this.eatOp(")")) {
          do {
            args.push(this.ternary());
          } while (this.eatOp(","));
          this.expectOp(")");
        }
        try {
          return (fn as (...a: unknown[]) => unknown)(...args);
        } catch (err) {
          if (err instanceof FormulaError) throw err;
          throw new FormulaError(`${name} failed: ${String(err)}`);
        }
      }
      if (Object.prototype.hasOwnProperty.call(this.ctx, name)) return this.ctx[name];
      throw new FormulaError(`unknown name: ${name}`);
    }
    throw new FormulaError("unexpected end of expression");
  }
}

function numericSafe(v: unknown): number | null {
  try {
    return numeric(v);
  } catch {
    return null;
  }
}

/**
 * Evaluate `formula` over `context` (names) and `functions` (callables).
 * Throws FormulaError for anything invalid, disallowed, or failing at runtime.
 */
export function evaluateFormulaSafely(
  formula: string,
  context: Record<string, unknown>,
  functions: Record<string, (...args: never[]) => unknown>
): unknown {
  if (typeof formula !== "string") throw new FormulaError("formula must be a string");
  if (formula.length > MAX_EXPRESSION_LENGTH) throw new FormulaError("formula too long");
  return new Parser(tokenize(formula), context, functions).parse();
}
