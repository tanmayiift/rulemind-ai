"""Turn a spreadsheet grid into a decision table (parity P07).

WHY THE BACKEND DOES NOT PARSE THE WORKBOOK. A pricing matrix arrives as .xlsx, and the obvious
move is `openpyxl` behind an upload endpoint. Three reasons this takes a grid of already-parsed
cells instead:

  1. The parser is ALREADY on the client. `apps/web/app/simulation/page.tsx` dynamically imports
     SheetJS to turn an uploaded workbook into decision payloads. A second parser, in a second
     language, for the same file format, is a second set of quirks to reconcile.
  2. Memory. The deployed API runs on 512MB with a resident sklearn/lightgbm stack (see
     `the compose `migrate` service`); openpyxl holds a workbook in memory and a 20MB sheet is not a small ask.
  3. Attack surface. OOXML is a zip of XML. Handing an untrusted upload to a Python XML parser on
     the API process is a decompression-bomb and XXE surface that a list of JSON rows is not.

So the binary stops at the browser and the interesting part -- turning `">= 1000"` in a cell into
`{"operator": ">=", "value": 1000}` -- lives here, where it can be tested without fixtures.

AMBIGUITY FAILS LOUDLY. Every cell this cannot parse is reported with its row and column and the
import is refused. The tempting alternative -- skip what you cannot read -- is silently WIDENING:
a dropped cell is a wildcard, and a wildcard matches everything, so an unreadable condition on a
pricing row would quietly apply that price to every input. Refusing an import is recoverable;
shipping a table that is broader than the spreadsheet it came from is not.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from .decision_tables import HIT_POLICIES, VALID_OUTCOMES

# Cells that mean "this dimension does not constrain this row".
WILDCARD_CELLS = {"", "*", "-", "any", "all", "n/a", "na", "—", "–"}

MAX_ROWS = 5000
MAX_COLUMNS = 60

# Longest first: ">=" must win over ">", and "!=" over "!". A dict would not preserve that, and
# the bug it produces is subtle -- ">= 700" parsed as ">" with a value of "= 700".
_COMPARISONS: Tuple[Tuple[str, str], ...] = (
    (">=", ">="), ("<=", "<="), ("!=", "!="), ("<>", "!="),
    ("==", "=="), ("=", "=="), (">", ">"), ("<", "<"),
)

_BETWEEN_PATTERNS = (
    # "600 - 749", "600..749", "600 to 749", "[600, 749]"
    re.compile(r"^\[?\s*(-?[\d.]+)\s*(?:\.\.|to|–|—|-)\s*(-?[\d.]+)\s*\]?$", re.IGNORECASE),
)

# The keyword must be followed by whitespace or an opening bracket. Without that delimiter the
# `\s*` was optional, so `in` matched the first two letters of an ordinary word: "India" parsed
# as `in ["dia"]` and "Interest" as `in ["terest"]`. A Region column full of country names would
# have been quietly turned into set-membership tests against truncated strings, and those rows
# would simply never match -- no error, no gap report, just a matrix that does nothing.
_SET_PATTERN = re.compile(r"^(not\s+in|in)(?=[\s\[(])\s*(.+)$", re.IGNORECASE)


class ImportError_(ValueError):
    """Raised with every problem found, not just the first.

    One-at-a-time errors turn a 40-row spreadsheet into 40 round trips. `problems` is the list the
    API hands back so the author fixes the sheet once.
    """

    def __init__(self, problems: List[Dict[str, Any]]):
        self.problems = problems
        super().__init__("; ".join(p["message"] for p in problems[:5]))


def _number(text: str) -> Optional[float]:
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    # 12.0 -> 12: a spreadsheet writes integers as floats, and a threshold rendered as "700.0" in
    # the authoring UI reads as a rounding artefact the author did not write.
    return int(value) if value.is_integer() else value


def _scalar(raw: Any) -> Any:
    """A cell value as the table should store it: number if numeric, bool if boolean-ish, else str."""
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return int(raw) if isinstance(raw, float) and float(raw).is_integer() else raw
    text = str(raw).strip()
    lowered = text.lower()
    if lowered in {"true", "yes"}:
        return True
    if lowered in {"false", "no"}:
        return False
    number = _number(text)
    return number if number is not None else text


def parse_cell(raw: Any) -> Optional[Dict[str, Any]]:
    """One spreadsheet cell -> one condition, or None for a wildcard.

    None is the wildcard and is CORRECT here -- `decision_tables._is_wildcard` treats a missing
    cell as "always matches", which is what an empty column means in a spreadsheet. It is only
    dangerous as a fallback for something unreadable, which is why unreadable raises instead.
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        return {"operator": "==", "value": raw}
    if isinstance(raw, (int, float)):
        return {"operator": "==", "value": _scalar(raw)}

    text = str(raw).strip()
    if text.lower() in WILDCARD_CELLS:
        return None

    for token, operator in _COMPARISONS:
        if text.startswith(token):
            rest = text[len(token):].strip()
            if not rest:
                raise ValueError("'{0}' has an operator but no value".format(text))
            return {"operator": operator, "value": _scalar(rest)}

    for pattern in _BETWEEN_PATTERNS:
        match = pattern.match(text)
        if match:
            low, high = _number(match.group(1)), _number(match.group(2))
            if low is None or high is None:
                raise ValueError("'{0}' looks like a range but its bounds are not numbers".format(text))
            if low > high:
                # Silently swapping would accept a typo and produce a row that matches nothing --
                # a gap the author never intended and would have to find by testing.
                raise ValueError("'{0}' is a backwards range ({1} > {2})".format(text, low, high))
            return {"operator": "between", "value": low, "value2": high}

    match = _SET_PATTERN.match(text)
    if match:
        operator = "not_in" if match.group(1).lower().startswith("not") else "in"
        # Brackets stripped here rather than in the pattern: doing it in the regex meant the
        # delimiter alternation had to also consume "[", and the version that did got
        # `in [a, b]` wrong (it captured "[a" as the first member) while the version that did not
        # matched the "in" inside "India".
        body = match.group(2).strip().strip("[](){}")
        members = [_scalar(part) for part in re.split(r"[,|]", body) if part.strip()]
        if not members:
            raise ValueError("'{0}' is an empty set".format(text))
        return {"operator": operator, "value": members}

    # "gold|silver|bronze" -- how a set is usually written in a sheet, without the word "in".
    if "|" in text:
        members = [_scalar(part) for part in text.split("|") if part.strip()]
        if len(members) > 1:
            return {"operator": "in", "value": members}

    return {"operator": "==", "value": _scalar(text)}


def _infer_type(values: List[Any]) -> str:
    """The column's type, from what is actually in it.

    Needed because a variable often declares no `field_type` (the seeded catalogue declares none
    at all), and defaulting every column to "string" is not a harmless placeholder. It costs two
    things, and the second is the expensive one:

      * the authoring UI renders its cell widgets from `field_type`, so a numeric threshold
        column comes back as a text box; and
      * THE OPTIMISER GOES BLIND. `analyze_decision_table` reasons over intervals, and a
        string-typed column cannot be ordered -- so every row was reported "not analyzable" and
        the gap analysis found nothing. With the types read off the data, the same sheet reports
        three precise uncovered ranges. The feature whose whole selling point is "MECE reports
        the gaps in your pricing matrix" was silently reporting none.

    The vocabulary is the AUTHORING EDITOR's -- outcome / string / number / boolean. An earlier
    version wrote "value" for any non-outcome output, which is not in that list, so the editor's
    `<Select>` found no matching option and displayed the first one: a price column labelled
    "outcome" on screen while the stored data said otherwise. A type nobody can render is worse
    than a wrong guess, because the screen contradicts the database.
    """
    present = [v for v in values if v is not None and v != ""]
    if not present:
        return "string"
    if all(isinstance(v, bool) for v in present):
        return "boolean"
    # bool is a subclass of int, so the bool check has to come first or every boolean column
    # would be reported as a number.
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in present):
        return "number"
    return "string"


def build_table(name: str, columns: List[Dict[str, Any]], rows: List[Dict[str, Any]],
                hit_policy: str = "first",
                description: Optional[str] = None) -> Dict[str, Any]:
    """Grid + column mapping -> a decision table in the shape `decision_tables` evaluates.

    `columns` is the author's mapping of sheet headers onto the table: each entry names a
    `header`, a `role` of "input" or "output" (anything else is ignored, so a sheet may carry
    notes columns), and for an input the `variable_id` it binds to.
    """
    problems: List[Dict[str, Any]] = []

    if hit_policy not in HIT_POLICIES:
        problems.append({"message": "hit policy '{0}' is not one of {1}".format(
            hit_policy, sorted(HIT_POLICIES))})
    if len(rows) > MAX_ROWS:
        problems.append({"message": "{0} rows exceeds the {1}-row limit".format(len(rows), MAX_ROWS)})
    if len(columns) > MAX_COLUMNS:
        problems.append({"message": "{0} columns exceeds the {1}-column limit".format(
            len(columns), MAX_COLUMNS)})

    inputs: List[Dict[str, Any]] = []
    outputs: List[Dict[str, Any]] = []
    # header -> (kind, id). Built before any row is read so a row can be mapped in one pass.
    binding: Dict[str, Tuple[str, str]] = {}

    for index, column in enumerate(columns):
        header = str(column.get("header") or "").strip()
        role = str(column.get("role") or "").strip().lower()
        if not header:
            problems.append({"column": index, "message": "column {0} has no header".format(index + 1)})
            continue
        if header in binding:
            # Two columns of the same name would silently overwrite each other in the row dicts,
            # so the second dimension of the matrix would vanish without a word.
            problems.append({"column": index, "header": header,
                             "message": "duplicate column header '{0}'".format(header)})
            continue
        if role == "input":
            variable_id = str(column.get("variable_id") or "").strip()
            if not variable_id:
                problems.append({"column": index, "header": header,
                                 "message": "input column '{0}' is not bound to a variable".format(header)})
                continue
            column_id = "in_{0}".format(_slug(header))
            declared = str(column.get("field_type") or "").strip()
            # None, not "string": filled in from the data below once the rows are parsed.
            inputs.append({"id": column_id, "variable_id": variable_id, "name": header,
                           "field_type": declared or None})
            binding[header] = ("input", column_id)
        elif role == "output":
            column_id = "out_{0}".format(_slug(header))
            declared = str(column.get("type") or "").strip()
            outputs.append({"id": column_id, "name": header, "type": declared or None})
            binding[header] = ("output", column_id)

    if not inputs:
        problems.append({"message": "no input columns -- a matrix needs at least one dimension"})
    if not outputs:
        problems.append({"message": "no output columns -- a matrix has to produce something"})

    outcome_ids = {o["id"] for o in outputs if str(o.get("type", "")).lower() == "outcome"}
    if len(outcome_ids) > 1:
        problems.append({"message": "more than one outcome column; a row can only resolve to one"})

    built_rows: List[Dict[str, Any]] = []
    observed: Dict[str, List[Any]] = {}
    for row_index, row in enumerate(rows):
        cells: Dict[str, Any] = {}
        row_outputs: Dict[str, Any] = {}
        for header, (kind, column_id) in binding.items():
            raw = row.get(header)
            if kind == "input":
                try:
                    parsed = parse_cell(raw)
                except ValueError as error:
                    problems.append({"row": row_index + 1, "header": header,
                                     "message": "row {0}, column '{1}': {2}".format(
                                         row_index + 1, header, error)})
                    continue
                if parsed is not None:
                    cells[column_id] = parsed
                    observed.setdefault(column_id, []).append(parsed.get("value"))
            else:
                if raw is None or str(raw).strip() == "":
                    continue
                value = _scalar(raw)
                if column_id in outcome_ids and str(value).lower() not in VALID_OUTCOMES:
                    problems.append({"row": row_index + 1, "header": header,
                                     "message": "row {0}: outcome '{1}' is not one of {2}".format(
                                         row_index + 1, value, sorted(VALID_OUTCOMES))})
                    continue
                row_outputs[column_id] = str(value).lower() if column_id in outcome_ids else value
                observed.setdefault(column_id, []).append(value)

        if not row_outputs:
            problems.append({"row": row_index + 1,
                             "message": "row {0} produces no output".format(row_index + 1)})
            continue
        built_rows.append({"id": "r{0}".format(row_index + 1), "cells": cells, "outputs": row_outputs})

    if not built_rows and not problems:
        problems.append({"message": "the sheet has no data rows"})
    if problems:
        raise ImportError_(problems)

    for column in inputs:
        if not column["field_type"]:
            # A `between` cell contributes only its low bound to `observed`, which is enough:
            # both bounds are required to be numbers for the cell to parse at all.
            column["field_type"] = _infer_type(observed.get(column["id"], []))
    for column in outputs:
        if not column["type"]:
            column["type"] = _infer_type(observed.get(column["id"], []))

    return {
        "name": name,
        "description": description,
        "hit_policy": hit_policy,
        "inputs": inputs,
        "outputs": outputs,
        "rows": built_rows,
        "default_row": None,
    }


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", str(text).strip().lower()).strip("_")
    return slug or "col"
