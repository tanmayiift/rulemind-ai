"""Generate packages/shared/excel-financial-date.spec.json from the Python engine.

CUMIPMT and DAYS360 are named in an `engineSemantics: true` parity row -- the five engines must
agree on them -- and both were Python-only with zero tests anywhere. This corpus is what puts
them under the contract.

Generated rather than hand-written for the same reason as the scorecard corpus:
`cross-platform-parity.test.ts` is named for parity but only ever compared TypeScript against
numbers a human typed, which is how ten divergences from Python survived in it. Numbers a human
types agree with whatever the human believed.

Run from the repo root:  python apps/python-executor/simulation/gen_excel_financial_date_spec.py
"""
import json
import sys

sys.path.insert(0, "apps/python-executor")
from app.excel_functions import CUMIPMT, CUMPRINC, IPMT, PPMT, DAYS360

cases = []
# Financial: realistic loan shapes plus the edges (zero rate, type_=1, single period, start>end).
for rate, nper, pv, start, end, type_ in [
    (0.05 / 12, 360, 300000, 1, 12, 0), (0.05 / 12, 360, 300000, 13, 24, 0),
    (0.08 / 12, 60, 25000, 1, 60, 0),   (0.08 / 12, 60, 25000, 1, 1, 0),
    (0.0, 24, 12000, 1, 12, 0),         (0.05 / 12, 360, 300000, 1, 12, 1),
    (0.10, 10, 5000, 3, 7, 0),          (0.05 / 12, 360, 300000, 5, 4, 0),
    (0.02, 12, 1000, 1, 12, 1),         (0.15 / 12, 36, 8000, 6, 18, 0),
]:
    cases.append({"fn": "CUMIPMT", "args": [rate, nper, pv, start, end, type_],
                  "expected": CUMIPMT(rate, nper, pv, start, end, type_)})
    cases.append({"fn": "CUMPRINC", "args": [rate, nper, pv, start, end, type_],
                  "expected": CUMPRINC(rate, nper, pv, start, end, type_)})
for rate, per, nper, pv, type_ in [
    (0.05 / 12, 1, 360, 300000, 0), (0.05 / 12, 180, 360, 300000, 0),
    (0.05 / 12, 1, 360, 300000, 1), (0.05 / 12, 2, 360, 300000, 1),
    (0.0, 5, 24, 12000, 0),         (0.10, 10, 10, 5000, 0),
]:
    cases.append({"fn": "IPMT", "args": [rate, per, nper, pv, 0, type_],
                  "expected": IPMT(rate, per, nper, pv, 0, type_)})
    cases.append({"fn": "PPMT", "args": [rate, per, nper, pv, 0, type_],
                  "expected": PPMT(rate, per, nper, pv, 0, type_)})

# DAYS360: month ends, both conventions, and the US asymmetry that a port gets wrong.
dates = [("2024-01-30", "2024-03-31"), ("2024-01-31", "2024-03-31"), ("2024-02-29", "2024-03-30"),
         ("2024-01-01", "2024-12-31"), ("2023-12-31", "2024-01-31"), ("2024-03-31", "2024-01-30"),
         ("2024-05-31", "2024-06-30"), ("2024-01-15", "2024-01-15"), ("2020-02-29", "2021-02-28")]
for start, end in dates:
    for method in (False, True):
        cases.append({"fn": "DAYS360", "args": [start, end, method],
                      "expected": DAYS360(start, end, method)})

out = {"note": ("Generated from app/excel_functions.py, the declared source of truth. "
                "CUMIPMT and DAYS360 were Python-only with zero tests anywhere despite being an "
                "engineSemantics row in the parity matrix; this corpus is what puts them under "
                "the five-engine contract."),
       "cases": cases}
open("packages/shared/excel-financial-date.spec.json", "w").write(json.dumps(out, indent=2) + "\n")
print("  %d cases generated" % len(cases))
