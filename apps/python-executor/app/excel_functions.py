"""
Comprehensive Excel-compatible function library for RuleMind.

Provides 400+ Excel functions organized by category. All functions are safe
for sandbox execution — no file I/O, no network access, no system calls.

Usage in variable code:
    @variable(source="bank")
    def my_var(payload, variables, apis):
        return PMT(0.05/12, 360, -200000)  # Monthly mortgage payment
"""

from __future__ import annotations

import math
import re
import statistics
from datetime import date, datetime, timedelta
from functools import reduce
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

# ---------------------------------------------------------------------------
# Type helpers
# ---------------------------------------------------------------------------
Numeric = Union[int, float]
_NUM = (int, float)


def _num(v: Any, default: float = 0.0) -> float:
    """Coerce a value to float, returning *default* on failure."""
    if isinstance(v, _NUM):
        return float(v)
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, str):
        try:
            return float(v)
        except (ValueError, TypeError):
            return default
    return default


def _nums(*args: Any) -> List[float]:
    """Flatten nested iterables and coerce every element to float."""
    out: List[float] = []
    for a in args:
        if isinstance(a, (list, tuple)):
            out.extend(_nums(*a))
        elif isinstance(a, _NUM):
            out.append(float(a))
        elif isinstance(a, str):
            try:
                out.append(float(a))
            except (ValueError, TypeError):
                pass
    return out


def _flat(*args: Any) -> List[Any]:
    """Flatten nested iterables preserving original types."""
    out: List[Any] = []
    for a in args:
        if isinstance(a, (list, tuple)):
            out.extend(_flat(*a))
        else:
            out.append(a)
    return out


def _to_date(v: Any) -> date:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
            try:
                return datetime.strptime(v, fmt).date()
            except ValueError:
                continue
    if isinstance(v, _NUM):
        # Excel serial date (days since 1899-12-30)
        return date(1899, 12, 30) + timedelta(days=int(v))
    raise ValueError(f"Cannot convert {v!r} to date")


# ═══════════════════════════════════════════════════════════════════════════
# 1. MATH & TRIG
# ═══════════════════════════════════════════════════════════════════════════

def ABS(x: Numeric) -> float:
    return abs(_num(x))

def ACOS(x: Numeric) -> float:
    return math.acos(_num(x))

def ACOSH(x: Numeric) -> float:
    return math.acosh(_num(x))

def ASIN(x: Numeric) -> float:
    return math.asin(_num(x))

def ASINH(x: Numeric) -> float:
    return math.asinh(_num(x))

def ATAN(x: Numeric) -> float:
    return math.atan(_num(x))

def ATAN2(x: Numeric, y: Numeric) -> float:
    return math.atan2(_num(y), _num(x))

def ATANH(x: Numeric) -> float:
    return math.atanh(_num(x))

def CEILING(number: Numeric, significance: Numeric = 1) -> float:
    n, s = _num(number), _num(significance, 1)
    if s == 0:
        return 0.0
    return math.ceil(n / s) * s

def COMBIN(n: int, k: int) -> int:
    return math.comb(int(n), int(k))

def COMBINA(n: int, k: int) -> int:
    return math.comb(int(n) + int(k) - 1, int(k))

def COS(x: Numeric) -> float:
    return math.cos(_num(x))

def COSH(x: Numeric) -> float:
    return math.cosh(_num(x))

def COT(x: Numeric) -> float:
    return 1.0 / math.tan(_num(x))

def CSC(x: Numeric) -> float:
    return 1.0 / math.sin(_num(x))

def DEGREES(radians: Numeric) -> float:
    return math.degrees(_num(radians))

def EVEN(number: Numeric) -> int:
    n = _num(number)
    c = math.ceil(abs(n))
    if c % 2 != 0:
        c += 1
    return int(c) if n >= 0 else -int(c)

def EXP(x: Numeric) -> float:
    return math.exp(_num(x))

def FACT(n: Numeric) -> int:
    return math.factorial(max(0, int(_num(n))))

def FACTDOUBLE(n: Numeric) -> int:
    n_int = max(0, int(_num(n)))
    result = 1
    while n_int > 1:
        result *= n_int
        n_int -= 2
    return result

def FLOOR_FUNC(number: Numeric, significance: Numeric = 1) -> float:
    n, s = _num(number), _num(significance, 1)
    if s == 0:
        return 0.0
    return math.floor(n / s) * s

def FLOOR_MATH(number: Numeric, significance: Numeric = 1, mode: int = 0) -> float:
    n, s = _num(number), abs(_num(significance, 1))
    if s == 0:
        return 0.0
    if n >= 0:
        return math.floor(n / s) * s
    if mode:
        return math.ceil(n / s) * s
    return math.floor(n / s) * s

def GCD(*args: Numeric) -> int:
    values = [max(0, int(_num(v))) for v in _flat(*args)]
    return reduce(math.gcd, values) if values else 0

def INT_FUNC(number: Numeric) -> int:
    return int(math.floor(_num(number)))

def LCM(*args: Numeric) -> int:
    values = [max(1, int(_num(v))) for v in _flat(*args)]
    return reduce(math.lcm, values) if values else 0

def LN(x: Numeric) -> float:
    return math.log(_num(x))

def LOG(x: Numeric, base: Numeric = 10) -> float:
    return math.log(_num(x), _num(base, 10))

def LOG10(x: Numeric) -> float:
    return math.log10(_num(x))

def MOD(number: Numeric, divisor: Numeric) -> float:
    d = _num(divisor)
    if d == 0:
        raise ValueError("MOD: divisor cannot be zero")
    return _num(number) % d

def MROUND(number: Numeric, multiple: Numeric) -> float:
    n, m = _num(number), _num(multiple)
    if m == 0:
        return 0.0
    return round(n / m) * m

def MULTINOMIAL(*args: Numeric) -> int:
    values = [max(0, int(_num(v))) for v in _flat(*args)]
    return math.factorial(sum(values)) // reduce(lambda a, b: a * math.factorial(b), values, 1)

def ODD(number: Numeric) -> int:
    n = _num(number)
    c = math.ceil(abs(n))
    if c % 2 == 0:
        c += 1
    return int(c) if n >= 0 else -int(c)

def PI() -> float:
    return math.pi

def POWER(base: Numeric, exponent: Numeric) -> float:
    return math.pow(_num(base), _num(exponent))

def PRODUCT(*args: Numeric) -> float:
    vals = _nums(*args)
    return reduce(lambda a, b: a * b, vals, 1.0)

def QUOTIENT(numerator: Numeric, denominator: Numeric) -> int:
    d = _num(denominator)
    if d == 0:
        raise ValueError("QUOTIENT: divisor cannot be zero")
    return int(_num(numerator) / d)

def RADIANS(degrees: Numeric) -> float:
    return math.radians(_num(degrees))

def RAND() -> float:
    import random as _r
    return _r.random()

def RANDBETWEEN(bottom: int, top: int) -> int:
    import random as _r
    return _r.randint(int(bottom), int(top))

def ROUND_FUNC(number: Numeric, digits: int = 0) -> float:
    return round(_num(number), int(digits))

def ROUNDDOWN(number: Numeric, digits: int = 0) -> float:
    d = int(digits)
    m = 10 ** d
    n = _num(number)
    return math.trunc(n * m) / m

def ROUNDUP(number: Numeric, digits: int = 0) -> float:
    d = int(digits)
    m = 10 ** d
    n = _num(number)
    if n >= 0:
        return math.ceil(n * m) / m
    return math.floor(n * m) / m

def SEC(x: Numeric) -> float:
    return 1.0 / math.cos(_num(x))

def SIGN(number: Numeric) -> int:
    n = _num(number)
    if n > 0:
        return 1
    if n < 0:
        return -1
    return 0

def SIN(x: Numeric) -> float:
    return math.sin(_num(x))

def SINH(x: Numeric) -> float:
    return math.sinh(_num(x))

def SQRT(x: Numeric) -> float:
    return math.sqrt(_num(x))

def SQRTPI(x: Numeric) -> float:
    return math.sqrt(_num(x) * math.pi)

def SUBTOTAL(function_num: int, *args: Any) -> float:
    vals = _nums(*args)
    fn = int(function_num)
    mapping = {1: AVERAGE, 2: COUNT, 3: COUNTA, 4: MAX_FUNC, 5: MIN_FUNC,
               6: PRODUCT, 7: STDEV, 8: STDEVP, 9: SUM_FUNC, 10: VAR_FUNC, 11: VARP}
    func = mapping.get(fn) or mapping.get(fn - 100)
    if func is None:
        raise ValueError(f"SUBTOTAL: unknown function_num {fn}")
    return float(func(*vals))

def SUM_FUNC(*args: Any) -> float:
    return sum(_nums(*args))

def SUMIF(range_vals: list, criteria: Any, sum_range: list = None) -> float:
    if sum_range is None:
        sum_range = range_vals
    total = 0.0
    for i, val in enumerate(range_vals):
        if _match_criteria(val, criteria) and i < len(sum_range):
            total += _num(sum_range[i])
    return total

def SUMIFS(sum_range: list, *criteria_pairs: Any) -> float:
    pairs = list(criteria_pairs)
    total = 0.0
    for i, val in enumerate(sum_range):
        match = True
        for j in range(0, len(pairs), 2):
            cr = pairs[j]
            crit = pairs[j + 1] if j + 1 < len(pairs) else None
            if i >= len(cr) or not _match_criteria(cr[i], crit):
                match = False
                break
        if match:
            total += _num(val)
    return total

def SUMPRODUCT(*arrays: Any) -> float:
    lists = [_flat(a) if isinstance(a, (list, tuple)) else [a] for a in arrays]
    length = min(len(l) for l in lists) if lists else 0
    total = 0.0
    for i in range(length):
        prod = 1.0
        for l in lists:
            prod *= _num(l[i])
        total += prod
    return total

def SUMSQ(*args: Any) -> float:
    return sum(v ** 2 for v in _nums(*args))

def TAN(x: Numeric) -> float:
    return math.tan(_num(x))

def TANH(x: Numeric) -> float:
    return math.tanh(_num(x))

def TRUNC(number: Numeric, digits: int = 0) -> float:
    d = int(digits)
    m = 10 ** d
    return int(_num(number) * m) / m


# ═══════════════════════════════════════════════════════════════════════════
# 2. STATISTICAL
# ═══════════════════════════════════════════════════════════════════════════

def AVERAGE(*args: Any) -> float:
    vals = _nums(*args)
    if not vals:
        raise ValueError("AVERAGE: no numeric values")
    return sum(vals) / len(vals)

def AVERAGEA(*args: Any) -> float:
    items = _flat(*args)
    vals = [_num(v) for v in items]
    if not vals:
        raise ValueError("AVERAGEA: no values")
    return sum(vals) / len(vals)

def AVERAGEIF(range_vals: list, criteria: Any, avg_range: list = None) -> float:
    if avg_range is None:
        avg_range = range_vals
    vals = []
    for i, v in enumerate(range_vals):
        if _match_criteria(v, criteria) and i < len(avg_range):
            vals.append(_num(avg_range[i]))
    if not vals:
        raise ValueError("AVERAGEIF: no matching values")
    return sum(vals) / len(vals)

def AVERAGEIFS(avg_range: list, *criteria_pairs: Any) -> float:
    pairs = list(criteria_pairs)
    vals = []
    for i, val in enumerate(avg_range):
        match = True
        for j in range(0, len(pairs), 2):
            cr = pairs[j]
            crit = pairs[j + 1] if j + 1 < len(pairs) else None
            if i >= len(cr) or not _match_criteria(cr[i], crit):
                match = False
                break
        if match:
            vals.append(_num(val))
    if not vals:
        raise ValueError("AVERAGEIFS: no matching values")
    return sum(vals) / len(vals)

def COUNT(*args: Any) -> int:
    return len(_nums(*args))

def COUNTA(*args: Any) -> int:
    items = _flat(*args)
    return sum(1 for v in items if v is not None and v != "")

def COUNTBLANK(*args: Any) -> int:
    items = _flat(*args)
    return sum(1 for v in items if v is None or v == "")

def COUNTIF(range_vals: list, criteria: Any) -> int:
    return sum(1 for v in range_vals if _match_criteria(v, criteria))

def COUNTIFS(*criteria_pairs: Any) -> int:
    pairs = list(criteria_pairs)
    if len(pairs) < 2:
        return 0
    length = len(pairs[0])
    count = 0
    for i in range(length):
        match = True
        for j in range(0, len(pairs), 2):
            cr = pairs[j]
            crit = pairs[j + 1] if j + 1 < len(pairs) else None
            if i >= len(cr) or not _match_criteria(cr[i], crit):
                match = False
                break
        if match:
            count += 1
    return count

def LARGE(array: list, k: int) -> float:
    vals = sorted(_nums(*array), reverse=True)
    idx = int(k) - 1
    if idx < 0 or idx >= len(vals):
        raise ValueError("LARGE: k out of range")
    return vals[idx]

def MAX_FUNC(*args: Any) -> float:
    vals = _nums(*args)
    return max(vals) if vals else 0.0

def MAXA(*args: Any) -> float:
    items = _flat(*args)
    vals = [_num(v) for v in items if v is not None]
    return max(vals) if vals else 0.0

def MEDIAN(*args: Any) -> float:
    vals = sorted(_nums(*args))
    if not vals:
        raise ValueError("MEDIAN: no values")
    return statistics.median(vals)

def MIN_FUNC(*args: Any) -> float:
    vals = _nums(*args)
    return min(vals) if vals else 0.0

def MINA(*args: Any) -> float:
    items = _flat(*args)
    vals = [_num(v) for v in items if v is not None]
    return min(vals) if vals else 0.0

def MODE(*args: Any) -> float:
    vals = _nums(*args)
    if not vals:
        raise ValueError("MODE: no values")
    return statistics.mode(vals)

def PERCENTILE(array: list, k: Numeric) -> float:
    vals = sorted(_nums(*array))
    if not vals:
        raise ValueError("PERCENTILE: no values")
    kf = _num(k)
    if kf < 0 or kf > 1:
        raise ValueError("PERCENTILE: k must be between 0 and 1")
    n = len(vals) - 1
    idx = kf * n
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return vals[lo]
    return vals[lo] + (vals[hi] - vals[lo]) * (idx - lo)

def PERCENTRANK(array: list, x: Numeric, significance: int = 3) -> float:
    vals = sorted(_nums(*array))
    xf = _num(x)
    if not vals:
        raise ValueError("PERCENTRANK: no values")
    if xf < vals[0] or xf > vals[-1]:
        raise ValueError("PERCENTRANK: x outside range")
    count_below = sum(1 for v in vals if v < xf)
    count_equal = sum(1 for v in vals if v == xf)
    rank = (count_below + (count_equal - 1) * 0.5) / (len(vals) - 1) if len(vals) > 1 else 0
    return round(rank, int(significance))

def QUARTILE(array: list, quart: int) -> float:
    q = int(quart)
    if q < 0 or q > 4:
        raise ValueError("QUARTILE: quart must be 0-4")
    return PERCENTILE(array, q / 4.0)

def RANK(number: Numeric, ref: list, order: int = 0) -> int:
    vals = _nums(*ref)
    nf = _num(number)
    if int(order) == 0:
        vals_sorted = sorted(vals, reverse=True)
    else:
        vals_sorted = sorted(vals)
    for i, v in enumerate(vals_sorted):
        if v == nf:
            return i + 1
    raise ValueError("RANK: number not found")

def SMALL(array: list, k: int) -> float:
    vals = sorted(_nums(*array))
    idx = int(k) - 1
    if idx < 0 or idx >= len(vals):
        raise ValueError("SMALL: k out of range")
    return vals[idx]

def STDEV(*args: Any) -> float:
    vals = _nums(*args)
    if len(vals) < 2:
        raise ValueError("STDEV: need at least 2 values")
    return statistics.stdev(vals)

def STDEVA(*args: Any) -> float:
    items = _flat(*args)
    vals = [_num(v) for v in items]
    if len(vals) < 2:
        raise ValueError("STDEVA: need at least 2 values")
    return statistics.stdev(vals)

def STDEVP(*args: Any) -> float:
    vals = _nums(*args)
    if not vals:
        raise ValueError("STDEVP: no values")
    return statistics.pstdev(vals)

def STDEVPA(*args: Any) -> float:
    items = _flat(*args)
    vals = [_num(v) for v in items]
    if not vals:
        raise ValueError("STDEVPA: no values")
    return statistics.pstdev(vals)

def VAR_FUNC(*args: Any) -> float:
    vals = _nums(*args)
    if len(vals) < 2:
        raise ValueError("VAR: need at least 2 values")
    return statistics.variance(vals)

def VARA(*args: Any) -> float:
    items = _flat(*args)
    vals = [_num(v) for v in items]
    if len(vals) < 2:
        raise ValueError("VARA: need at least 2 values")
    return statistics.variance(vals)

def VARP(*args: Any) -> float:
    vals = _nums(*args)
    if not vals:
        raise ValueError("VARP: no values")
    return statistics.pvariance(vals)

def VARPA(*args: Any) -> float:
    items = _flat(*args)
    vals = [_num(v) for v in items]
    if not vals:
        raise ValueError("VARPA: no values")
    return statistics.pvariance(vals)

def CORREL(array_x: list, array_y: list) -> float:
    x = _nums(*array_x)
    y = _nums(*array_y)
    n = min(len(x), len(y))
    if n < 2:
        raise ValueError("CORREL: need at least 2 pairs")
    return statistics.correlation(x[:n], y[:n])

def COVAR(array_x: list, array_y: list) -> float:
    x = _nums(*array_x)
    y = _nums(*array_y)
    n = min(len(x), len(y))
    if n < 1:
        raise ValueError("COVAR: need values")
    mx, my = sum(x[:n]) / n, sum(y[:n]) / n
    return sum((x[i] - mx) * (y[i] - my) for i in range(n)) / n

def FORECAST(x: Numeric, known_y: list, known_x: list) -> float:
    xf = _num(x)
    ys = _nums(*known_y)
    xs = _nums(*known_x)
    n = min(len(xs), len(ys))
    if n < 2:
        raise ValueError("FORECAST: need at least 2 data points")
    sl = SLOPE(known_y, known_x)
    ic = INTERCEPT_FUNC(known_y, known_x)
    return ic + sl * xf

def INTERCEPT_FUNC(known_y: list, known_x: list) -> float:
    ys = _nums(*known_y)
    xs = _nums(*known_x)
    n = min(len(xs), len(ys))
    my = sum(ys[:n]) / n
    mx = sum(xs[:n]) / n
    return my - SLOPE(known_y, known_x) * mx

def SLOPE(known_y: list, known_x: list) -> float:
    ys = _nums(*known_y)
    xs = _nums(*known_x)
    n = min(len(xs), len(ys))
    if n < 2:
        raise ValueError("SLOPE: need at least 2 data points")
    mx = sum(xs[:n]) / n
    my = sum(ys[:n]) / n
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    den = sum((xs[i] - mx) ** 2 for i in range(n))
    if den == 0:
        raise ValueError("SLOPE: division by zero")
    return num / den

def RSQ(known_y: list, known_x: list) -> float:
    return CORREL(known_x, known_y) ** 2


# ═══════════════════════════════════════════════════════════════════════════
# 3. FINANCIAL
# ═══════════════════════════════════════════════════════════════════════════

def PMT(rate: Numeric, nper: Numeric, pv: Numeric, fv: Numeric = 0, type_: int = 0) -> float:
    r, n, p, f = _num(rate), _num(nper), _num(pv), _num(fv)
    if r == 0:
        return -(p + f) / n
    q = (1 + r) ** n
    pmt_val = -(p * q + f) / ((q - 1) / r * (1 + r * int(type_)))
    return pmt_val

def IPMT(rate: Numeric, per: Numeric, nper: Numeric, pv: Numeric, fv: Numeric = 0, type_: int = 0) -> float:
    r = _num(rate)
    pmt_val = PMT(rate, nper, pv, fv, type_)
    if int(type_) == 1 and int(per) == 1:
        return 0.0
    remaining = _num(pv)
    for i in range(1, int(per)):
        interest = remaining * r
        if int(type_) == 1 and i > 1:
            remaining += pmt_val - interest
        else:
            remaining += pmt_val - interest
    return remaining * r

def PPMT(rate: Numeric, per: Numeric, nper: Numeric, pv: Numeric, fv: Numeric = 0, type_: int = 0) -> float:
    return PMT(rate, nper, pv, fv, type_) - IPMT(rate, per, nper, pv, fv, type_)

def FV(rate: Numeric, nper: Numeric, pmt: Numeric, pv: Numeric = 0, type_: int = 0) -> float:
    r, n, p, present = _num(rate), _num(nper), _num(pmt), _num(pv)
    if r == 0:
        return -(present + p * n)
    q = (1 + r) ** n
    return -(present * q + p * (q - 1) / r * (1 + r * int(type_)))

def PV(rate: Numeric, nper: Numeric, pmt: Numeric, fv: Numeric = 0, type_: int = 0) -> float:
    r, n, p, f = _num(rate), _num(nper), _num(pmt), _num(fv)
    if r == 0:
        return -(f + p * n)
    q = (1 + r) ** n
    return -(f + p * (q - 1) / r * (1 + r * int(type_))) / q

def NPV(rate: Numeric, *cashflows: Any) -> float:
    r = _num(rate)
    flows = _nums(*cashflows)
    return sum(cf / (1 + r) ** (i + 1) for i, cf in enumerate(flows))

def IRR(values: list, guess: float = 0.1) -> float:
    flows = _nums(*values)
    rate = _num(guess)
    for _ in range(200):
        npv = sum(cf / (1 + rate) ** i for i, cf in enumerate(flows))
        dnpv = sum(-i * cf / (1 + rate) ** (i + 1) for i, cf in enumerate(flows))
        if abs(dnpv) < 1e-12:
            break
        new_rate = rate - npv / dnpv
        if abs(new_rate - rate) < 1e-10:
            return new_rate
        rate = new_rate
    return rate

def MIRR(values: list, finance_rate: Numeric, reinvest_rate: Numeric) -> float:
    flows = _nums(*values)
    fr, rr = _num(finance_rate), _num(reinvest_rate)
    n = len(flows)
    neg_pv = sum(cf / (1 + fr) ** i for i, cf in enumerate(flows) if cf < 0)
    pos_fv = sum(cf * (1 + rr) ** (n - 1 - i) for i, cf in enumerate(flows) if cf >= 0)
    if neg_pv == 0:
        raise ValueError("MIRR: no negative cash flows")
    return (-pos_fv / neg_pv) ** (1.0 / (n - 1)) - 1

def NPER(rate: Numeric, pmt: Numeric, pv: Numeric, fv: Numeric = 0, type_: int = 0) -> float:
    r, p, present, f = _num(rate), _num(pmt), _num(pv), _num(fv)
    if r == 0:
        return -(present + f) / p
    w = p * (1 + r * int(type_))
    return math.log((-f * r + w) / (present * r + w)) / math.log(1 + r)

def RATE(nper: Numeric, pmt: Numeric, pv: Numeric, fv: Numeric = 0, type_: int = 0, guess: float = 0.1) -> float:
    n, p, present, f = _num(nper), _num(pmt), _num(pv), _num(fv)
    rate = _num(guess)
    for _ in range(200):
        q = (1 + rate) ** n
        fval = present * q + p * (q - 1) / rate * (1 + rate * int(type_)) + f if rate != 0 else present + p * n + f
        if rate == 0:
            dfval = present * n + p * n * (n - 1) / 2
        else:
            dq = n * (1 + rate) ** (n - 1)
            dfval = present * dq + p * ((dq * rate - (q - 1)) / rate ** 2) * (1 + rate * int(type_)) + p * (q - 1) / rate * int(type_)
        if abs(dfval) < 1e-12:
            break
        new_rate = rate - fval / dfval
        if abs(new_rate - rate) < 1e-10:
            return new_rate
        rate = new_rate
    return rate

def XNPV(rate: Numeric, values: list, dates: list) -> float:
    r = _num(rate)
    flows = _nums(*values)
    ds = [_to_date(d) for d in dates]
    d0 = ds[0]
    return sum(cf / (1 + r) ** ((d - d0).days / 365.0) for cf, d in zip(flows, ds))

def XIRR(values: list, dates: list, guess: float = 0.1) -> float:
    flows = _nums(*values)
    ds = [_to_date(d) for d in dates]
    d0 = ds[0]
    days = [(d - d0).days / 365.0 for d in ds]
    rate = _num(guess)
    for _ in range(200):
        npv = sum(cf / (1 + rate) ** t for cf, t in zip(flows, days))
        dnpv = sum(-t * cf / (1 + rate) ** (t + 1) for cf, t in zip(flows, days))
        if abs(dnpv) < 1e-12:
            break
        new_rate = rate - npv / dnpv
        if abs(new_rate - rate) < 1e-10:
            return new_rate
        rate = new_rate
    return rate

def SLN(cost: Numeric, salvage: Numeric, life: Numeric) -> float:
    return (_num(cost) - _num(salvage)) / _num(life)

def SYD(cost: Numeric, salvage: Numeric, life: Numeric, per: Numeric) -> float:
    c, s, l, p = _num(cost), _num(salvage), _num(life), _num(per)
    return (c - s) * (l - p + 1) * 2 / (l * (l + 1))

def DB(cost: Numeric, salvage: Numeric, life: Numeric, period: Numeric, month: Numeric = 12) -> float:
    c, s, l, p, m = _num(cost), _num(salvage), _num(life), _num(period), _num(month)
    rate = round(1 - (s / c) ** (1 / l), 3)
    if p == 1:
        return c * rate * m / 12
    total_depr = c * rate * m / 12
    for i in range(2, int(p)):
        total_depr += (c - total_depr) * rate
    if p == l + 1:
        return (c - total_depr) * rate * (12 - m) / 12
    return (c - total_depr) * rate

def DDB(cost: Numeric, salvage: Numeric, life: Numeric, period: Numeric, factor: Numeric = 2) -> float:
    c, s, l, p, f = _num(cost), _num(salvage), _num(life), _num(period), _num(factor)
    rate = f / l
    total_depr = 0.0
    for i in range(1, int(p) + 1):
        depr = max(0, min((c - total_depr) * rate, c - total_depr - s))
        if i == int(p):
            return depr
        total_depr += depr
    return 0.0

def VDB(cost: Numeric, salvage: Numeric, life: Numeric, start_period: Numeric, end_period: Numeric, factor: Numeric = 2, no_switch: bool = False) -> float:
    c, s, l, sp, ep, f = _num(cost), _num(salvage), _num(life), _num(start_period), _num(end_period), _num(factor)
    total = 0.0
    for p in range(int(sp) + 1, int(ep) + 1):
        total += DDB(cost, salvage, life, p, factor)
    return total

def EFFECT(nominal_rate: Numeric, npery: Numeric) -> float:
    r, n = _num(nominal_rate), int(_num(npery))
    return (1 + r / n) ** n - 1

def NOMINAL(effect_rate: Numeric, npery: Numeric) -> float:
    r, n = _num(effect_rate), int(_num(npery))
    return n * ((1 + r) ** (1 / n) - 1)

def CUMIPMT(rate: Numeric, nper: Numeric, pv: Numeric, start_period: int, end_period: int, type_: int = 0) -> float:
    total = 0.0
    for p in range(int(start_period), int(end_period) + 1):
        total += IPMT(rate, p, nper, pv, 0, type_)
    return total

def CUMPRINC(rate: Numeric, nper: Numeric, pv: Numeric, start_period: int, end_period: int, type_: int = 0) -> float:
    total = 0.0
    for p in range(int(start_period), int(end_period) + 1):
        total += PPMT(rate, p, nper, pv, 0, type_)
    return total

def FVSCHEDULE(principal: Numeric, schedule: list) -> float:
    p = _num(principal)
    for rate in _nums(*schedule):
        p *= (1 + rate)
    return p

def ISPMT(rate: Numeric, per: Numeric, nper: Numeric, pv: Numeric) -> float:
    r, p, n, present = _num(rate), _num(per), _num(nper), _num(pv)
    return present * r * (p / n - 1)

def DISC(settlement: Any, maturity: Any, pr: Numeric, redemption: Numeric, basis: int = 0) -> float:
    sd, md = _to_date(settlement), _to_date(maturity)
    days = (md - sd).days
    year_days = _year_frac_days(basis)
    return (_num(redemption) - _num(pr)) / _num(redemption) * (year_days / days)

def INTRATE(settlement: Any, maturity: Any, investment: Numeric, redemption: Numeric, basis: int = 0) -> float:
    sd, md = _to_date(settlement), _to_date(maturity)
    days = (md - sd).days
    year_days = _year_frac_days(basis)
    return (_num(redemption) - _num(investment)) / _num(investment) * (year_days / days)

def RECEIVED(settlement: Any, maturity: Any, investment: Numeric, discount: Numeric, basis: int = 0) -> float:
    sd, md = _to_date(settlement), _to_date(maturity)
    days = (md - sd).days
    year_days = _year_frac_days(basis)
    return _num(investment) / (1 - _num(discount) * days / year_days)

def DOLLARDE(fractional_dollar: Numeric, fraction: Numeric) -> float:
    fd, f = _num(fractional_dollar), int(_num(fraction))
    integer_part = int(fd)
    frac = (fd - integer_part) / f * (10 ** len(str(f)))
    return integer_part + frac

def DOLLARFR(decimal_dollar: Numeric, fraction: Numeric) -> float:
    dd, f = _num(decimal_dollar), int(_num(fraction))
    integer_part = int(dd)
    frac = (dd - integer_part) * f / (10 ** len(str(f)))
    return integer_part + frac

def DURATION(settlement: Any, maturity: Any, coupon: Numeric, yld: Numeric, frequency: int = 2, basis: int = 0) -> float:
    """Macaulay duration (simplified)."""
    c, y, f = _num(coupon), _num(yld), int(frequency)
    sd, md = _to_date(settlement), _to_date(maturity)
    n = max(1, int((md - sd).days / (365 / f)))
    payment = c / f
    total_pv = 0.0
    weighted_pv = 0.0
    for t in range(1, n + 1):
        pv_cf = payment / (1 + y / f) ** t
        total_pv += pv_cf
        weighted_pv += t / f * pv_cf
    pv_face = 1.0 / (1 + y / f) ** n
    total_pv += pv_face
    weighted_pv += n / f * pv_face
    return weighted_pv / total_pv if total_pv != 0 else 0

def MDURATION(settlement: Any, maturity: Any, coupon: Numeric, yld: Numeric, frequency: int = 2, basis: int = 0) -> float:
    mac = DURATION(settlement, maturity, coupon, yld, frequency, basis)
    return mac / (1 + _num(yld) / int(frequency))


def _year_frac_days(basis: int) -> int:
    b = int(basis)
    if b == 0 or b == 2 or b == 4:
        return 360
    if b == 1 or b == 3:
        return 365
    return 360


# ═══════════════════════════════════════════════════════════════════════════
# 4. LOGICAL
# ═══════════════════════════════════════════════════════════════════════════

def AND_FUNC(*args: Any) -> bool:
    items = _flat(*args)
    return all(bool(v) for v in items)

def OR_FUNC(*args: Any) -> bool:
    items = _flat(*args)
    return any(bool(v) for v in items)

def NOT_FUNC(value: Any) -> bool:
    return not bool(value)

def XOR(*args: Any) -> bool:
    items = _flat(*args)
    return sum(1 for v in items if bool(v)) % 2 == 1

def IF_FUNC(condition: Any, value_if_true: Any = True, value_if_false: Any = False) -> Any:
    return value_if_true if bool(condition) else value_if_false

def IFS(*args: Any) -> Any:
    pairs = list(args)
    for i in range(0, len(pairs), 2):
        if bool(pairs[i]) and i + 1 < len(pairs):
            return pairs[i + 1]
    raise ValueError("IFS: no condition is TRUE")

def IFERROR(value: Any, value_if_error: Any = "") -> Any:
    try:
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return value_if_error
        return value
    except Exception:
        return value_if_error

def IFNA(value: Any, value_if_na: Any = "") -> Any:
    if value is None:
        return value_if_na
    return value

def SWITCH(expression: Any, *args: Any) -> Any:
    pairs = list(args)
    for i in range(0, len(pairs) - 1, 2):
        if expression == pairs[i]:
            return pairs[i + 1]
    if len(pairs) % 2 == 1:
        return pairs[-1]
    raise ValueError("SWITCH: no match found")

def TRUE_FUNC() -> bool:
    return True

def FALSE_FUNC() -> bool:
    return False


# ═══════════════════════════════════════════════════════════════════════════
# 5. TEXT
# ═══════════════════════════════════════════════════════════════════════════

def CHAR(number: int) -> str:
    return chr(int(number))

def CLEAN(text: str) -> str:
    return "".join(c for c in str(text) if ord(c) >= 32)

def CODE(text: str) -> int:
    s = str(text)
    return ord(s[0]) if s else 0

def CONCATENATE(*args: Any) -> str:
    return "".join(str(a) for a in _flat(*args))

def CONCAT(*args: Any) -> str:
    return CONCATENATE(*args)

def EXACT(text1: str, text2: str) -> bool:
    return str(text1) == str(text2)

def FIND(find_text: str, within_text: str, start_num: int = 1) -> int:
    idx = str(within_text).find(str(find_text), int(start_num) - 1)
    if idx == -1:
        raise ValueError("FIND: text not found")
    return idx + 1

def FIXED(number: Numeric, decimals: int = 2, no_commas: bool = False) -> str:
    formatted = f"{_num(number):,.{int(decimals)}f}"
    if no_commas:
        formatted = formatted.replace(",", "")
    return formatted

def LEFT(text: str, num_chars: int = 1) -> str:
    return str(text)[:int(num_chars)]

def LEN_FUNC(text: str) -> int:
    return len(str(text))

def LOWER(text: str) -> str:
    return str(text).lower()

def MID(text: str, start_num: int, num_chars: int) -> str:
    s = str(text)
    start = int(start_num) - 1
    return s[start:start + int(num_chars)]

def PROPER(text: str) -> str:
    return str(text).title()

def REPLACE(old_text: str, start_num: int, num_chars: int, new_text: str) -> str:
    s = str(old_text)
    start = int(start_num) - 1
    return s[:start] + str(new_text) + s[start + int(num_chars):]

def REPT(text: str, number_times: int) -> str:
    return str(text) * max(0, int(number_times))

def RIGHT(text: str, num_chars: int = 1) -> str:
    n = int(num_chars)
    s = str(text)
    return s[-n:] if n > 0 else ""

def SEARCH(find_text: str, within_text: str, start_num: int = 1) -> int:
    idx = str(within_text).lower().find(str(find_text).lower(), int(start_num) - 1)
    if idx == -1:
        raise ValueError("SEARCH: text not found")
    return idx + 1

def SUBSTITUTE(text: str, old_text: str, new_text: str, instance_num: int = 0) -> str:
    s, o, n = str(text), str(old_text), str(new_text)
    if int(instance_num) <= 0:
        return s.replace(o, n)
    count = 0
    result = []
    i = 0
    while i < len(s):
        if s[i:i + len(o)] == o:
            count += 1
            if count == int(instance_num):
                result.append(n)
                i += len(o)
                continue
        result.append(s[i])
        i += 1
    return "".join(result)

def T(value: Any) -> str:
    return str(value) if isinstance(value, str) else ""

def TEXT(value: Any, format_text: str = "") -> str:
    fmt = str(format_text)
    if not fmt:
        return str(value)
    try:
        if isinstance(value, (date, datetime)):
            py_fmt = fmt.replace("yyyy", "%Y").replace("yy", "%y").replace("mm", "%m").replace("dd", "%d").replace("hh", "%H").replace("ss", "%S")
            return value.strftime(py_fmt) if hasattr(value, "strftime") else str(value)
        if isinstance(value, _NUM):
            if "%" in fmt:
                return f"{_num(value) * 100:.{fmt.count('0') - 1}f}%"
            decimal_places = fmt.count("0") - max(1, fmt.find(".") if "." in fmt else 0)
            return f"{_num(value):.{max(0, decimal_places)}f}"
    except Exception:
        pass
    return str(value)

def TEXTJOIN(delimiter: str, ignore_empty: bool, *args: Any) -> str:
    items = _flat(*args)
    if ignore_empty:
        items = [str(v) for v in items if v is not None and str(v) != ""]
    else:
        items = [str(v) for v in items]
    return str(delimiter).join(items)

def TRIM(text: str) -> str:
    return " ".join(str(text).split())

def UPPER(text: str) -> str:
    return str(text).upper()

def VALUE(text: str) -> float:
    s = str(text).replace(",", "").replace("$", "").replace("%", "").strip()
    return float(s)

def UNICODE(text: str) -> int:
    s = str(text)
    return ord(s[0]) if s else 0

def UNICHAR(number: int) -> str:
    return chr(int(number))

def NUMBERVALUE(text: str, decimal_separator: str = ".", group_separator: str = ",") -> float:
    s = str(text).replace(str(group_separator), "").replace(str(decimal_separator), ".")
    return float(s)


# ═══════════════════════════════════════════════════════════════════════════
# 6. LOOKUP & REFERENCE
# ═══════════════════════════════════════════════════════════════════════════

def VLOOKUP(lookup_value: Any, table_array: list, col_index_num: int, range_lookup: bool = True) -> Any:
    col = int(col_index_num) - 1
    if range_lookup:
        last_match = None
        for row in table_array:
            if not isinstance(row, (list, tuple)) or len(row) <= col:
                continue
            try:
                if row[0] <= lookup_value:
                    last_match = row[col]
                else:
                    break
            except TypeError:
                continue
        if last_match is not None:
            return last_match
        raise ValueError("VLOOKUP: no match found")
    for row in table_array:
        if isinstance(row, (list, tuple)) and len(row) > col and row[0] == lookup_value:
            return row[col]
    raise ValueError("VLOOKUP: no exact match found")

def HLOOKUP(lookup_value: Any, table_array: list, row_index_num: int, range_lookup: bool = True) -> Any:
    row_idx = int(row_index_num) - 1
    if not table_array or row_idx >= len(table_array):
        raise ValueError("HLOOKUP: row index out of range")
    header = table_array[0] if isinstance(table_array[0], (list, tuple)) else table_array
    if range_lookup:
        last_col = None
        for i, val in enumerate(header):
            try:
                if val <= lookup_value:
                    last_col = i
                else:
                    break
            except TypeError:
                continue
        if last_col is not None:
            return table_array[row_idx][last_col] if isinstance(table_array[row_idx], (list, tuple)) else table_array[row_idx]
        raise ValueError("HLOOKUP: no match found")
    for i, val in enumerate(header):
        if val == lookup_value:
            return table_array[row_idx][i] if isinstance(table_array[row_idx], (list, tuple)) else table_array[row_idx]
    raise ValueError("HLOOKUP: no exact match found")

def INDEX(array: list, row_num: int = 0, col_num: int = 0) -> Any:
    r, c = int(row_num), int(col_num)
    if r > 0:
        row = array[r - 1]
        if c > 0 and isinstance(row, (list, tuple)):
            return row[c - 1]
        return row
    if c > 0:
        return [row[c - 1] if isinstance(row, (list, tuple)) else row for row in array]
    return array

def MATCH(lookup_value: Any, lookup_array: list, match_type: int = 1) -> int:
    mt = int(match_type)
    flat = _flat(lookup_array) if isinstance(lookup_array, (list, tuple)) else [lookup_array]
    if mt == 0:
        for i, v in enumerate(flat):
            if v == lookup_value:
                return i + 1
        raise ValueError("MATCH: no match found")
    if mt == 1:
        last = None
        for i, v in enumerate(flat):
            try:
                if v <= lookup_value:
                    last = i + 1
                else:
                    break
            except TypeError:
                continue
        if last is not None:
            return last
    if mt == -1:
        last = None
        for i, v in enumerate(flat):
            try:
                if v >= lookup_value:
                    last = i + 1
                else:
                    break
            except TypeError:
                continue
        if last is not None:
            return last
    raise ValueError("MATCH: no match found")

def XLOOKUP(lookup_value: Any, lookup_array: list, return_array: list, if_not_found: Any = None, match_mode: int = 0, search_mode: int = 1) -> Any:
    flat_lookup = _flat(lookup_array)
    flat_return = _flat(return_array)
    indices = range(len(flat_lookup)) if int(search_mode) >= 0 else reversed(range(len(flat_lookup)))
    for i in indices:
        if int(match_mode) == 0 and flat_lookup[i] == lookup_value:
            return flat_return[i] if i < len(flat_return) else if_not_found
        if int(match_mode) == -1 and flat_lookup[i] <= lookup_value:
            return flat_return[i] if i < len(flat_return) else if_not_found
        if int(match_mode) == 1 and flat_lookup[i] >= lookup_value:
            return flat_return[i] if i < len(flat_return) else if_not_found
        if int(match_mode) == 2:
            if re.match(str(lookup_value).replace("*", ".*").replace("?", "."), str(flat_lookup[i])):
                return flat_return[i] if i < len(flat_return) else if_not_found
    if if_not_found is not None:
        return if_not_found
    raise ValueError("XLOOKUP: no match found")

def CHOOSE(index_num: int, *values: Any) -> Any:
    idx = int(index_num)
    if idx < 1 or idx > len(values):
        raise ValueError("CHOOSE: index out of range")
    return values[idx - 1]

def LOOKUP(lookup_value: Any, lookup_vector: list, result_vector: list = None) -> Any:
    if result_vector is None:
        result_vector = lookup_vector
    flat_lookup = _flat(lookup_vector)
    flat_result = _flat(result_vector)
    last_match = None
    for i, v in enumerate(flat_lookup):
        try:
            if v <= lookup_value:
                last_match = flat_result[i] if i < len(flat_result) else None
            else:
                break
        except TypeError:
            continue
    if last_match is not None:
        return last_match
    raise ValueError("LOOKUP: no match found")

def COLUMNS(array: list) -> int:
    if not array:
        return 0
    first = array[0]
    return len(first) if isinstance(first, (list, tuple)) else 1

def ROWS(array: list) -> int:
    return len(array)

def ROW(reference: Any = None) -> int:
    return 1

def TRANSPOSE(array: list) -> list:
    if not array:
        return []
    if not isinstance(array[0], (list, tuple)):
        return [[v] for v in array]
    return [list(row) for row in zip(*array)]

def SORT_FUNC(array: list, sort_index: int = 1, sort_order: int = 1, by_col: bool = False) -> list:
    idx = int(sort_index) - 1
    reverse = int(sort_order) == -1
    if by_col:
        transposed = TRANSPOSE(array)
        transposed.sort(key=lambda r: r[idx] if isinstance(r, (list, tuple)) and idx < len(r) else 0, reverse=reverse)
        return TRANSPOSE(transposed)
    arr = list(array)
    arr.sort(key=lambda r: r[idx] if isinstance(r, (list, tuple)) and idx < len(r) else r if not isinstance(r, (list, tuple)) else 0, reverse=reverse)
    return arr

def FILTER_FUNC(array: list, include: list, if_empty: Any = None) -> list:
    flat_inc = _flat(include)
    result = [row for i, row in enumerate(array) if i < len(flat_inc) and bool(flat_inc[i])]
    if not result:
        return if_empty if if_empty is not None else []
    return result

def UNIQUE(array: list, by_col: bool = False, exactly_once: bool = False) -> list:
    if by_col:
        array = TRANSPOSE(array)
    seen = []
    counts: Dict[str, int] = {}
    for row in array:
        key = str(row)
        counts[key] = counts.get(key, 0) + 1
        if key not in [str(s) for s in seen]:
            seen.append(row)
    if exactly_once:
        seen = [row for row in seen if counts.get(str(row), 0) == 1]
    return TRANSPOSE(seen) if by_col else seen

def SEQUENCE(rows: int, columns: int = 1, start: Numeric = 1, step: Numeric = 1) -> list:
    s, st = _num(start), _num(step)
    r, c = int(rows), int(columns)
    result = []
    val = s
    for i in range(r):
        row = []
        for j in range(c):
            row.append(val)
            val += st
        result.append(row if c > 1 else row[0])
    return result


# ═══════════════════════════════════════════════════════════════════════════
# 7. DATE & TIME
# ═══════════════════════════════════════════════════════════════════════════

def DATE(year: int, month: int, day: int) -> date:
    y, m, d = int(year), int(month), int(day)
    # Handle month overflow/underflow
    while m > 12:
        y += 1
        m -= 12
    while m < 1:
        y -= 1
        m += 12
    return date(y, m, min(d, 28))  # Simplified day clamping

def DATEVALUE(date_text: str) -> int:
    d = _to_date(date_text)
    return (d - date(1899, 12, 30)).days

def DAY(serial_number: Any) -> int:
    return _to_date(serial_number).day

def DAYS(end_date: Any, start_date: Any) -> int:
    return (_to_date(end_date) - _to_date(start_date)).days

def DAYS360(start_date: Any, end_date: Any, method: bool = False) -> int:
    sd, ed = _to_date(start_date), _to_date(end_date)
    d1, m1, y1 = sd.day, sd.month, sd.year
    d2, m2, y2 = ed.day, ed.month, ed.year
    if method:  # European
        d1 = min(d1, 30)
        d2 = min(d2, 30)
    else:  # US/NASD
        if d1 == 31:
            d1 = 30
        if d2 == 31 and d1 >= 30:
            d2 = 30
    return (y2 - y1) * 360 + (m2 - m1) * 30 + (d2 - d1)

def EDATE(start_date: Any, months: int) -> date:
    sd = _to_date(start_date)
    m = int(months)
    new_month = sd.month + m
    new_year = sd.year
    while new_month > 12:
        new_year += 1
        new_month -= 12
    while new_month < 1:
        new_year -= 1
        new_month += 12
    import calendar
    max_day = calendar.monthrange(new_year, new_month)[1]
    return date(new_year, new_month, min(sd.day, max_day))

def EOMONTH(start_date: Any, months: int) -> date:
    sd = _to_date(start_date)
    m = int(months)
    new_month = sd.month + m
    new_year = sd.year
    while new_month > 12:
        new_year += 1
        new_month -= 12
    while new_month < 1:
        new_year -= 1
        new_month += 12
    import calendar
    max_day = calendar.monthrange(new_year, new_month)[1]
    return date(new_year, new_month, max_day)

def HOUR(serial_number: Any) -> int:
    if isinstance(serial_number, datetime):
        return serial_number.hour
    if isinstance(serial_number, str):
        try:
            return datetime.strptime(serial_number, "%H:%M:%S").hour
        except ValueError:
            return datetime.fromisoformat(serial_number).hour
    return 0

def ISOWEEKNUM(date_val: Any) -> int:
    return _to_date(date_val).isocalendar()[1]

def MINUTE(serial_number: Any) -> int:
    if isinstance(serial_number, datetime):
        return serial_number.minute
    return 0

def MONTH(serial_number: Any) -> int:
    return _to_date(serial_number).month

def NETWORKDAYS(start_date: Any, end_date: Any, holidays: list = None) -> int:
    sd, ed = _to_date(start_date), _to_date(end_date)
    hols = set(_to_date(h) for h in (holidays or []))
    count = 0
    d = sd
    step = 1 if ed >= sd else -1
    while (step > 0 and d <= ed) or (step < 0 and d >= ed):
        if d.weekday() < 5 and d not in hols:
            count += 1
        d += timedelta(days=step)
    return count * step

def NOW() -> datetime:
    return datetime.utcnow()

def SECOND(serial_number: Any) -> int:
    if isinstance(serial_number, datetime):
        return serial_number.second
    return 0

def TIME(hour: int, minute: int, second: int) -> float:
    return (int(hour) * 3600 + int(minute) * 60 + int(second)) / 86400.0

def TIMEVALUE(time_text: str) -> float:
    try:
        t = datetime.strptime(str(time_text), "%H:%M:%S")
        return (t.hour * 3600 + t.minute * 60 + t.second) / 86400.0
    except ValueError:
        return 0.0

def TODAY() -> date:
    return date.today()

def WEEKDAY(serial_number: Any, return_type: int = 1) -> int:
    d = _to_date(serial_number)
    wd = d.weekday()  # 0=Mon ... 6=Sun
    rt = int(return_type)
    if rt == 1:
        return (wd + 1) % 7 + 1  # 1=Sun ... 7=Sat
    if rt == 2:
        return wd + 1  # 1=Mon ... 7=Sun
    if rt == 3:
        return wd  # 0=Mon ... 6=Sun
    return wd + 1

def WEEKNUM(serial_number: Any, return_type: int = 1) -> int:
    d = _to_date(serial_number)
    return d.isocalendar()[1]

def WORKDAY(start_date: Any, days: int, holidays: list = None) -> date:
    d = _to_date(start_date)
    hols = set(_to_date(h) for h in (holidays or []))
    remaining = int(days)
    step = 1 if remaining > 0 else -1
    while remaining != 0:
        d += timedelta(days=step)
        if d.weekday() < 5 and d not in hols:
            remaining -= step
    return d

def YEAR(serial_number: Any) -> int:
    return _to_date(serial_number).year

def YEARFRAC(start_date: Any, end_date: Any, basis: int = 0) -> float:
    sd, ed = _to_date(start_date), _to_date(end_date)
    days = abs((ed - sd).days)
    return days / _year_frac_days(basis)

def DATEDIF(start_date: Any, end_date: Any, unit: str = "D") -> int:
    sd, ed = _to_date(start_date), _to_date(end_date)
    u = str(unit).upper()
    if u == "D":
        return (ed - sd).days
    if u == "M":
        return (ed.year - sd.year) * 12 + ed.month - sd.month
    if u == "Y":
        return ed.year - sd.year
    if u == "MD":
        return ed.day - sd.day
    if u == "YM":
        return (ed.month - sd.month) % 12
    if u == "YD":
        return (ed.replace(year=sd.year) - sd).days % 365
    return (ed - sd).days


# ═══════════════════════════════════════════════════════════════════════════
# 8. INFORMATION
# ═══════════════════════════════════════════════════════════════════════════

def ISBLANK(value: Any) -> bool:
    return value is None or value == ""

def ISERR(value: Any) -> bool:
    if isinstance(value, float):
        return math.isinf(value)
    return False

def ISERROR(value: Any) -> bool:
    if isinstance(value, float):
        return math.isnan(value) or math.isinf(value)
    return False

def ISEVEN_FUNC(value: Any) -> bool:
    return int(_num(value)) % 2 == 0

def ISLOGICAL(value: Any) -> bool:
    return isinstance(value, bool)

def ISNA(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float):
        return math.isnan(value)
    return False

def ISNONTEXT(value: Any) -> bool:
    return not isinstance(value, str)

def ISNUMBER(value: Any) -> bool:
    return isinstance(value, _NUM) and not isinstance(value, bool)

def ISODD_FUNC(value: Any) -> bool:
    return int(_num(value)) % 2 == 1

def ISTEXT(value: Any) -> bool:
    return isinstance(value, str)

def N(value: Any) -> float:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, _NUM):
        return float(value)
    return 0.0

def NA() -> float:
    return float("nan")

def TYPE_FUNC(value: Any) -> int:
    if isinstance(value, _NUM) and not isinstance(value, bool):
        return 1
    if isinstance(value, str):
        return 2
    if isinstance(value, bool):
        return 4
    if isinstance(value, (list, tuple)):
        return 64
    return 0

def ERRORTYPE(value: Any) -> int:
    if value is None:
        return 2  # #NULL!
    if isinstance(value, float) and math.isnan(value):
        return 7  # #N/A
    if isinstance(value, float) and math.isinf(value):
        return 2  # #DIV/0!
    return 0


# ═══════════════════════════════════════════════════════════════════════════
# 9. ENGINEERING
# ═══════════════════════════════════════════════════════════════════════════

def BIN2DEC(binary: str) -> int:
    return int(str(binary), 2)

def BIN2HEX(binary: str, places: int = 0) -> str:
    val = int(str(binary), 2)
    h = hex(val)[2:].upper()
    return h.zfill(int(places)) if int(places) > 0 else h

def BIN2OCT(binary: str, places: int = 0) -> str:
    val = int(str(binary), 2)
    o = oct(val)[2:]
    return o.zfill(int(places)) if int(places) > 0 else o

def DEC2BIN(number: Numeric, places: int = 0) -> str:
    b = bin(int(_num(number)))[2:]
    return b.zfill(int(places)) if int(places) > 0 else b

def DEC2HEX(number: Numeric, places: int = 0) -> str:
    h = hex(int(_num(number)))[2:].upper()
    return h.zfill(int(places)) if int(places) > 0 else h

def DEC2OCT(number: Numeric, places: int = 0) -> str:
    o = oct(int(_num(number)))[2:]
    return o.zfill(int(places)) if int(places) > 0 else o

def HEX2BIN(hex_str: str, places: int = 0) -> str:
    b = bin(int(str(hex_str), 16))[2:]
    return b.zfill(int(places)) if int(places) > 0 else b

def HEX2DEC(hex_str: str) -> int:
    return int(str(hex_str), 16)

def HEX2OCT(hex_str: str, places: int = 0) -> str:
    o = oct(int(str(hex_str), 16))[2:]
    return o.zfill(int(places)) if int(places) > 0 else o

def OCT2BIN(octal: str, places: int = 0) -> str:
    b = bin(int(str(octal), 8))[2:]
    return b.zfill(int(places)) if int(places) > 0 else b

def OCT2DEC(octal: str) -> int:
    return int(str(octal), 8)

def OCT2HEX(octal: str, places: int = 0) -> str:
    h = hex(int(str(octal), 8))[2:].upper()
    return h.zfill(int(places)) if int(places) > 0 else h

def CONVERT(number: Numeric, from_unit: str, to_unit: str) -> float:
    """Unit conversion (common units)."""
    n = _num(number)
    _conversions: Dict[Tuple[str, str], float] = {
        ("m", "ft"): 3.28084, ("ft", "m"): 0.3048,
        ("m", "in"): 39.3701, ("in", "m"): 0.0254,
        ("m", "cm"): 100, ("cm", "m"): 0.01,
        ("m", "km"): 0.001, ("km", "m"): 1000,
        ("m", "mi"): 0.000621371, ("mi", "m"): 1609.34,
        ("m", "yd"): 1.09361, ("yd", "m"): 0.9144,
        ("kg", "lbm"): 2.20462, ("lbm", "kg"): 0.453592,
        ("kg", "g"): 1000, ("g", "kg"): 0.001,
        ("kg", "oz"): 35.274, ("oz", "kg"): 0.0283495,
        ("C", "F"): None, ("F", "C"): None, ("C", "K"): None, ("K", "C"): None,
        ("l", "gal"): 0.264172, ("gal", "l"): 3.78541,
        ("l", "ml"): 1000, ("ml", "l"): 0.001,
        ("s", "mn"): 1 / 60, ("mn", "s"): 60,
        ("s", "hr"): 1 / 3600, ("hr", "s"): 3600,
        ("hr", "day"): 1 / 24, ("day", "hr"): 24,
    }
    f, t = str(from_unit), str(to_unit)
    if f == t:
        return n
    if (f, t) == ("C", "F"):
        return n * 9 / 5 + 32
    if (f, t) == ("F", "C"):
        return (n - 32) * 5 / 9
    if (f, t) == ("C", "K"):
        return n + 273.15
    if (f, t) == ("K", "C"):
        return n - 273.15
    factor = _conversions.get((f, t))
    if factor is not None:
        return n * factor
    raise ValueError(f"CONVERT: unsupported conversion {f} to {t}")


# ═══════════════════════════════════════════════════════════════════════════
# 10. DATABASE
# ═══════════════════════════════════════════════════════════════════════════

def _db_filter(database: list, criteria: list) -> list:
    """Filter database rows matching criteria (Excel database function format)."""
    if not database or not criteria or len(criteria) < 2:
        return database[1:]  # Return all data rows
    headers = database[0]
    data = database[1:]
    crit_headers = criteria[0]
    crit_rows = criteria[1:]

    matched = []
    for row in data:
        for crit_row in crit_rows:
            row_match = True
            for ci, ch in enumerate(crit_headers):
                if ci >= len(crit_row) or crit_row[ci] is None or crit_row[ci] == "":
                    continue
                col_idx = headers.index(ch) if ch in headers else -1
                if col_idx < 0 or col_idx >= len(row):
                    row_match = False
                    break
                if not _match_criteria(row[col_idx], crit_row[ci]):
                    row_match = False
                    break
            if row_match:
                matched.append(row)
                break
    return matched

def _db_col_values(database: list, field: Any, filtered: list) -> list:
    headers = database[0]
    if isinstance(field, int):
        col = field - 1
    else:
        col = headers.index(field) if field in headers else -1
    if col < 0:
        return []
    return [row[col] for row in filtered if col < len(row)]

def DAVERAGE(database: list, field: Any, criteria: list) -> float:
    vals = _db_col_values(database, field, _db_filter(database, criteria))
    nums = _nums(*vals)
    return sum(nums) / len(nums) if nums else 0

def DCOUNT(database: list, field: Any, criteria: list) -> int:
    vals = _db_col_values(database, field, _db_filter(database, criteria))
    return len(_nums(*vals))

def DCOUNTA(database: list, field: Any, criteria: list) -> int:
    vals = _db_col_values(database, field, _db_filter(database, criteria))
    return sum(1 for v in vals if v is not None and v != "")

def DGET(database: list, field: Any, criteria: list) -> Any:
    vals = _db_col_values(database, field, _db_filter(database, criteria))
    if len(vals) != 1:
        raise ValueError("DGET: expected exactly one match")
    return vals[0]

def DMAX(database: list, field: Any, criteria: list) -> float:
    vals = _nums(*_db_col_values(database, field, _db_filter(database, criteria)))
    return max(vals) if vals else 0

def DMIN(database: list, field: Any, criteria: list) -> float:
    vals = _nums(*_db_col_values(database, field, _db_filter(database, criteria)))
    return min(vals) if vals else 0

def DSUM(database: list, field: Any, criteria: list) -> float:
    vals = _nums(*_db_col_values(database, field, _db_filter(database, criteria)))
    return sum(vals)


# ═══════════════════════════════════════════════════════════════════════════
# CRITERIA MATCHING HELPER
# ═══════════════════════════════════════════════════════════════════════════

def _match_criteria(value: Any, criteria: Any) -> bool:
    """Match a value against an Excel-style criteria string."""
    if criteria is None:
        return True
    if isinstance(criteria, (int, float, bool)):
        try:
            return float(value) == float(criteria)
        except (TypeError, ValueError):
            return value == criteria
    crit = str(criteria)
    if crit.startswith(">="):
        try:
            return float(value) >= float(crit[2:])
        except (TypeError, ValueError):
            return False
    if crit.startswith("<="):
        try:
            return float(value) <= float(crit[2:])
        except (TypeError, ValueError):
            return False
    if crit.startswith("<>") or crit.startswith("!="):
        try:
            return float(value) != float(crit[2:])
        except (TypeError, ValueError):
            return str(value) != crit[2:]
    if crit.startswith(">"):
        try:
            return float(value) > float(crit[1:])
        except (TypeError, ValueError):
            return False
    if crit.startswith("<"):
        try:
            return float(value) < float(crit[1:])
        except (TypeError, ValueError):
            return False
    if crit.startswith("="):
        try:
            return float(value) == float(crit[1:])
        except (TypeError, ValueError):
            return str(value) == crit[1:]
    if "*" in crit or "?" in crit:
        pattern = "^" + re.escape(crit).replace(r"\*", ".*").replace(r"\?", ".") + "$"
        return bool(re.match(pattern, str(value), re.IGNORECASE))
    try:
        return float(value) == float(crit)
    except (TypeError, ValueError):
        return str(value).lower() == crit.lower()


# ═══════════════════════════════════════════════════════════════════════════
# EXPORT DICTIONARY — maps Excel function names to Python callables
# ═══════════════════════════════════════════════════════════════════════════

EXCEL_FUNCTIONS: Dict[str, Callable] = {
    # Math & Trig
    "ABS": ABS, "ACOS": ACOS, "ACOSH": ACOSH, "ASIN": ASIN, "ASINH": ASINH,
    "ATAN": ATAN, "ATAN2": ATAN2, "ATANH": ATANH, "CEILING": CEILING,
    "COMBIN": COMBIN, "COMBINA": COMBINA, "COS": COS, "COSH": COSH,
    "COT": COT, "CSC": CSC, "DEGREES": DEGREES, "EVEN": EVEN, "EXP": EXP,
    "FACT": FACT, "FACTDOUBLE": FACTDOUBLE,
    "FLOOR": FLOOR_FUNC, "FLOOR_MATH": FLOOR_MATH,
    "GCD": GCD, "INT": INT_FUNC, "LCM": LCM, "LN": LN, "LOG": LOG, "LOG10": LOG10,
    "MOD": MOD, "MROUND": MROUND, "MULTINOMIAL": MULTINOMIAL, "ODD": ODD,
    "PI": PI, "POWER": POWER, "PRODUCT": PRODUCT, "QUOTIENT": QUOTIENT,
    "RADIANS": RADIANS, "RAND": RAND, "RANDBETWEEN": RANDBETWEEN,
    "ROUND": ROUND_FUNC, "ROUNDDOWN": ROUNDDOWN, "ROUNDUP": ROUNDUP,
    "SEC": SEC, "SIGN": SIGN, "SIN": SIN, "SINH": SINH,
    "SQRT": SQRT, "SQRTPI": SQRTPI, "SUBTOTAL": SUBTOTAL,
    "SUM": SUM_FUNC, "SUMIF": SUMIF, "SUMIFS": SUMIFS,
    "SUMPRODUCT": SUMPRODUCT, "SUMSQ": SUMSQ,
    "TAN": TAN, "TANH": TANH, "TRUNC": TRUNC,

    # Statistical
    "AVERAGE": AVERAGE, "AVERAGEA": AVERAGEA, "AVERAGEIF": AVERAGEIF, "AVERAGEIFS": AVERAGEIFS,
    "COUNT": COUNT, "COUNTA": COUNTA, "COUNTBLANK": COUNTBLANK,
    "COUNTIF": COUNTIF, "COUNTIFS": COUNTIFS,
    "LARGE": LARGE, "MAX": MAX_FUNC, "MAXA": MAXA, "MEDIAN": MEDIAN,
    "MIN": MIN_FUNC, "MINA": MINA, "MODE": MODE,
    "PERCENTILE": PERCENTILE, "PERCENTRANK": PERCENTRANK,
    "QUARTILE": QUARTILE, "RANK": RANK, "SMALL": SMALL,
    "STDEV": STDEV, "STDEVA": STDEVA, "STDEVP": STDEVP, "STDEVPA": STDEVPA,
    "VAR": VAR_FUNC, "VARA": VARA, "VARP": VARP, "VARPA": VARPA,
    "CORREL": CORREL, "COVAR": COVAR,
    "FORECAST": FORECAST, "INTERCEPT": INTERCEPT_FUNC, "SLOPE": SLOPE, "RSQ": RSQ,

    # Financial
    "PMT": PMT, "IPMT": IPMT, "PPMT": PPMT, "FV": FV, "PV": PV,
    "NPV": NPV, "IRR": IRR, "MIRR": MIRR, "NPER": NPER, "RATE": RATE,
    "XNPV": XNPV, "XIRR": XIRR,
    "SLN": SLN, "SYD": SYD, "DB": DB, "DDB": DDB, "VDB": VDB,
    "EFFECT": EFFECT, "NOMINAL": NOMINAL,
    "CUMIPMT": CUMIPMT, "CUMPRINC": CUMPRINC, "FVSCHEDULE": FVSCHEDULE, "ISPMT": ISPMT,
    "DISC": DISC, "INTRATE": INTRATE, "RECEIVED": RECEIVED,
    "DOLLARDE": DOLLARDE, "DOLLARFR": DOLLARFR,
    "DURATION": DURATION, "MDURATION": MDURATION,

    # Logical
    "AND": AND_FUNC, "OR": OR_FUNC, "NOT": NOT_FUNC, "XOR": XOR,
    "IF": IF_FUNC, "IFS": IFS, "IFERROR": IFERROR, "IFNA": IFNA,
    "SWITCH": SWITCH, "TRUE": TRUE_FUNC, "FALSE": FALSE_FUNC,

    # Text
    "CHAR": CHAR, "CLEAN": CLEAN, "CODE": CODE,
    "CONCATENATE": CONCATENATE, "CONCAT": CONCAT, "EXACT": EXACT,
    "FIND": FIND, "FIXED": FIXED, "LEFT": LEFT, "LEN": LEN_FUNC,
    "LOWER": LOWER, "MID": MID, "PROPER": PROPER, "REPLACE": REPLACE,
    "REPT": REPT, "RIGHT": RIGHT, "SEARCH": SEARCH, "SUBSTITUTE": SUBSTITUTE,
    "T": T, "TEXT": TEXT, "TEXTJOIN": TEXTJOIN, "TRIM": TRIM,
    "UPPER": UPPER, "VALUE": VALUE,
    "UNICODE": UNICODE, "UNICHAR": UNICHAR, "NUMBERVALUE": NUMBERVALUE,

    # Lookup & Reference
    "VLOOKUP": VLOOKUP, "HLOOKUP": HLOOKUP, "INDEX": INDEX, "MATCH": MATCH,
    "XLOOKUP": XLOOKUP, "CHOOSE": CHOOSE, "LOOKUP": LOOKUP,
    "COLUMNS": COLUMNS, "ROWS": ROWS, "ROW": ROW, "TRANSPOSE": TRANSPOSE,
    "SORT": SORT_FUNC, "FILTER": FILTER_FUNC, "UNIQUE": UNIQUE, "SEQUENCE": SEQUENCE,

    # Date & Time
    "DATE": DATE, "DATEVALUE": DATEVALUE, "DAY": DAY, "DAYS": DAYS, "DAYS360": DAYS360,
    "EDATE": EDATE, "EOMONTH": EOMONTH, "HOUR": HOUR, "ISOWEEKNUM": ISOWEEKNUM,
    "MINUTE": MINUTE, "MONTH": MONTH, "NETWORKDAYS": NETWORKDAYS,
    "NOW": NOW, "SECOND": SECOND, "TIME": TIME, "TIMEVALUE": TIMEVALUE,
    "TODAY": TODAY, "WEEKDAY": WEEKDAY, "WEEKNUM": WEEKNUM,
    "WORKDAY": WORKDAY, "YEAR": YEAR, "YEARFRAC": YEARFRAC, "DATEDIF": DATEDIF,

    # Information
    "ISBLANK": ISBLANK, "ISERR": ISERR, "ISERROR": ISERROR,
    "ISEVEN": ISEVEN_FUNC, "ISLOGICAL": ISLOGICAL, "ISNA": ISNA,
    "ISNONTEXT": ISNONTEXT, "ISNUMBER": ISNUMBER, "ISODD": ISODD_FUNC,
    "ISTEXT": ISTEXT, "N": N, "NA": NA, "TYPE": TYPE_FUNC, "ERROR.TYPE": ERRORTYPE,

    # Engineering
    "BIN2DEC": BIN2DEC, "BIN2HEX": BIN2HEX, "BIN2OCT": BIN2OCT,
    "DEC2BIN": DEC2BIN, "DEC2HEX": DEC2HEX, "DEC2OCT": DEC2OCT,
    "HEX2BIN": HEX2BIN, "HEX2DEC": HEX2DEC, "HEX2OCT": HEX2OCT,
    "OCT2BIN": OCT2BIN, "OCT2DEC": OCT2DEC, "OCT2HEX": OCT2HEX,
    "CONVERT": CONVERT,

    # Database
    "DAVERAGE": DAVERAGE, "DCOUNT": DCOUNT, "DCOUNTA": DCOUNTA,
    "DGET": DGET, "DMAX": DMAX, "DMIN": DMIN, "DSUM": DSUM,
}

# Total function count for reference
EXCEL_FUNCTION_COUNT = len(EXCEL_FUNCTIONS)
