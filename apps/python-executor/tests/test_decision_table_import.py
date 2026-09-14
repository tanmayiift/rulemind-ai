"""Importing a spreadsheet as a decision table (parity P07).

THE ROW THIS CLOSES WAS WRONG ABOUT ITS OWN SUBJECT. `parity.json` said P07 was ABSENT at ~4
weeks -- "no matrix entity, decision tables are flat row/column with no named dimensions". They
are not flat: `inputs` are named dimensions bound to variables, rows state a condition per
dimension, outputs are multiple and unconstrained except for the outcome column, and
`analyze_decision_table` already reports the uncovered input space. The only missing piece was
getting a spreadsheet in, which is what this covers.

WHAT THE TESTS ARE ACTUALLY ABOUT. Cell parsing, and specifically its FAILURE direction. A cell
this cannot read must refuse the import, because the alternative -- skipping it -- leaves no cell
for that dimension, and `_is_wildcard` treats a missing cell as "matches everything". An
unreadable condition on a pricing row would silently apply that price to every input in the
matrix. Widening a table is the dangerous direction, so most of the negative tests below are
about refusing rather than guessing.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
# Popped, not set: AUTH_MODE=none turns RBAC off entirely, and an authorization suite that runs
# without auth proves nothing.
os.environ.pop("AUTH_MODE", None)

from fastapi.testclient import TestClient  # noqa: E402

import app.main as app_main  # noqa: E402
from app.decision_table_import import ImportError_, build_table, parse_cell  # noqa: E402
from app.decision_tables import analyze_decision_table, evaluate_decision_table  # noqa: E402
from app.storage import Storage  # noqa: E402
from tests._storage_global import restore_storage_after  # noqa: E402

BASE = "/api/v1/decision-tables"

# A three-dimension pricing matrix -- the e2eFlow's own example, written the way a person writes
# a spreadsheet: comparisons, a range, a set, and a wildcard.
# NOTE the absence of `field_type` on the inputs and of `type` on Price. That is what the web
# actually sends -- it only forwards a type the VARIABLE declares, and the seeded catalogue
# declares none -- so the fixture has to match, or the inference tests below assert a value that
# was handed to them and cannot fail. They did, until a mutation refused to die.
COLUMNS = [
    {"header": "Region", "role": "input", "variable_id": "region"},
    {"header": "Tier", "role": "input", "variable_id": "tier"},
    {"header": "Volume", "role": "input", "variable_id": "volume"},
    {"header": "Price", "role": "output"},
    {"header": "Decision", "role": "output", "type": "outcome"},
    {"header": "Notes", "role": "note"},
]

ROWS = [
    {"Region": "APAC", "Tier": "gold|platinum", "Volume": ">= 1000", "Price": 9.5,
     "Decision": "approve", "Notes": "volume deal"},
    {"Region": "APAC", "Tier": "gold|platinum", "Volume": "100 - 999", "Price": 12.5,
     "Decision": "approve", "Notes": ""},
    {"Region": "EMEA", "Tier": "*", "Volume": "< 100", "Price": 18.0, "Decision": "review",
     "Notes": "small order"},
]


class CellParsingTest(unittest.TestCase):
    """The whole import is this function applied in a loop, so it is tested directly."""

    def test_comparisons(self):
        self.assertEqual(parse_cell(">= 700"), {"operator": ">=", "value": 700})
        self.assertEqual(parse_cell("<=0.5"), {"operator": "<=", "value": 0.5})
        self.assertEqual(parse_cell("> 10"), {"operator": ">", "value": 10})
        self.assertEqual(parse_cell("!= gold"), {"operator": "!=", "value": "gold"})
        self.assertEqual(parse_cell("<> gold"), {"operator": "!=", "value": "gold"})

    def test_the_longest_operator_wins(self):
        """`>=` must be tried before `>`. Matching `>` first parses ">= 700" as greater-than the
        string "= 700", which compares as a string and silently changes the threshold."""
        self.assertEqual(parse_cell(">= 700"), {"operator": ">=", "value": 700})
        self.assertNotEqual(parse_cell(">= 700")["operator"], ">")

    def test_ranges(self):
        for text in ("600 - 749", "600-749", "600..749", "600 to 749", "[600, 749]".replace(",", " -")):
            with self.subTest(text=text):
                self.assertEqual(parse_cell(text),
                                 {"operator": "between", "value": 600, "value2": 749})

    def test_a_backwards_range_is_refused_not_swapped(self):
        """Swapping would accept the typo and produce a row that matches nothing -- a gap the
        author never wrote and would only find by testing every combination."""
        with self.assertRaises(ValueError):
            parse_cell("749 - 600")

    def test_sets(self):
        self.assertEqual(parse_cell("gold|silver"), {"operator": "in", "value": ["gold", "silver"]})
        self.assertEqual(parse_cell("in [gold, silver]"),
                         {"operator": "in", "value": ["gold", "silver"]})
        self.assertEqual(parse_cell("not in [gold, silver]"),
                         {"operator": "not_in", "value": ["gold", "silver"]})

    def test_a_word_starting_with_in_is_not_a_set(self):
        """The bug this test exists for shipped in the first version and would never have raised
        anything: `in` matched the first two letters of an ordinary word, so a Region column of
        country names turned "India" into `in ["dia"]` -- a membership test against a truncated
        string that simply never matches. No error, no gap report, just a row that does nothing.
        The keyword needs a delimiter after it."""
        for text in ("india", "INDIA", "individual", "interest", "in", "not india"):
            with self.subTest(text=text):
                parsed = parse_cell(text)
                self.assertEqual(parsed["operator"], "==",
                                 "'{0}' was read as a set".format(text))
                self.assertEqual(parsed["value"], text)

    def test_set_brackets_are_stripped_not_absorbed_into_the_first_member(self):
        """The other half of the same fix. Consuming "[" in the delimiter alternation made
        `in [a,b]` capture "[a" as its first member -- which also never matches."""
        self.assertEqual(parse_cell("in [a,b]"), {"operator": "in", "value": ["a", "b"]})
        self.assertEqual(parse_cell("in(a, b)"), {"operator": "in", "value": ["a", "b"]})
        self.assertEqual(parse_cell("not in [a, b]"), {"operator": "not_in", "value": ["a", "b"]})

    def test_wildcards_become_no_cell_at_all(self):
        for text in ("", "*", "-", "any", "ALL", "n/a", "—"):
            with self.subTest(text=text):
                self.assertIsNone(parse_cell(text))
        self.assertIsNone(parse_cell(None))

    def test_bare_values_are_equality(self):
        self.assertEqual(parse_cell("APAC"), {"operator": "==", "value": "APAC"})
        self.assertEqual(parse_cell(700), {"operator": "==", "value": 700})
        self.assertEqual(parse_cell(True), {"operator": "==", "value": True})
        self.assertEqual(parse_cell("true"), {"operator": "==", "value": True})

    def test_a_spreadsheet_integer_does_not_become_a_float(self):
        """Excel hands back 700.0 for a cell containing 700. Storing that renders the threshold
        as "700.0" in the authoring UI, which reads as a rounding artefact the author did not
        write."""
        self.assertEqual(parse_cell(700.0), {"operator": "==", "value": 700})
        self.assertEqual(parse_cell(">= 700.0"), {"operator": ">=", "value": 700})

    def test_an_operator_with_no_value_is_refused(self):
        with self.assertRaises(ValueError):
            parse_cell(">=")


class BuildTableTest(unittest.TestCase):
    def test_a_three_dimension_matrix_round_trips_into_something_evaluable(self):
        """The e2eFlow, minus the HTTP: three dimensions in, a table out, and the table decides."""
        table = build_table("Pricing", COLUMNS, ROWS)
        self.assertEqual([i["name"] for i in table["inputs"]], ["Region", "Tier", "Volume"])
        self.assertEqual([i["variable_id"] for i in table["inputs"]], ["region", "tier", "volume"])
        self.assertEqual([o["name"] for o in table["outputs"]], ["Price", "Decision"])

        result = evaluate_decision_table(table, {"region": "APAC", "tier": "gold", "volume": 5000})
        self.assertEqual(result["outcome"], "approve")
        self.assertEqual(result["outputs"]["out_price"], 9.5)

        mid = evaluate_decision_table(table, {"region": "APAC", "tier": "platinum", "volume": 250})
        self.assertEqual(mid["outputs"]["out_price"], 12.5)

    def test_column_types_are_inferred_from_the_data_not_defaulted_to_string(self):
        """`field_type` drives the authoring UI's cell widgets, and the seeded variable catalogue
        declares none at all -- so defaulting every column to "string" turns a numeric threshold
        column into a text box. The values are right there in the sheet."""
        table = build_table("Pricing", COLUMNS, ROWS)
        types = {i["name"]: i["field_type"] for i in table["inputs"]}
        self.assertEqual(types["Volume"], "number", "a column of numbers came back as a string")
        self.assertEqual(types["Region"], "string")

    def test_an_output_type_is_one_the_editor_can_render(self):
        """The vocabulary is the editor's: outcome / string / number / boolean. Writing "value"
        (the first version did) leaves the editor's <Select> with no matching option, so it shows
        the FIRST one -- a price column labelled "outcome" on screen while the stored row says
        otherwise. A type nobody can render is worse than a wrong guess."""
        table = build_table("Pricing", COLUMNS, ROWS)
        types = {o["name"]: o["type"] for o in table["outputs"]}
        self.assertEqual(types["Price"], "number")
        self.assertEqual(types["Decision"], "outcome", "an explicit outcome type was overwritten")
        self.assertTrue(set(types.values()) <= {"outcome", "string", "number", "boolean"},
                        "an output carries a type the authoring editor has no option for")

    def test_a_declared_type_is_not_overwritten_by_inference(self):
        """Inference is a fallback, not an override: a variable that declares its type wins over
        whatever this particular sheet happens to contain."""
        columns = [dict(c) for c in COLUMNS]
        for column in columns:
            if column["header"] == "Volume":
                column["field_type"] = "string"
        types = {i["name"]: i["field_type"] for i in build_table("P", columns, ROWS)["inputs"]}
        self.assertEqual(types["Volume"], "string", "a declared type was overwritten")
        # The control: the SAME column infers "number" when nothing declares it, so the
        # assertion above is about precedence rather than about the default.
        self.assertEqual(
            {i["name"]: i["field_type"] for i in build_table("P", COLUMNS, ROWS)["inputs"]}["Volume"],
            "number")

    def test_a_boolean_column_is_not_reported_as_a_number(self):
        """`bool` is a subclass of `int`, so an isinstance check in the wrong order calls every
        boolean column a number."""
        columns = [{"header": "Flag", "role": "input", "variable_id": "flag"},
                   {"header": "Price", "role": "output"}]
        table = build_table("Flags", columns, [{"Flag": "true", "Price": 1},
                                               {"Flag": "false", "Price": 2}])
        self.assertEqual(table["inputs"][0]["field_type"], "boolean")

    def test_a_wildcard_dimension_really_does_match_anything(self):
        table = build_table("Pricing", COLUMNS, ROWS)
        emea = evaluate_decision_table(table, {"region": "EMEA", "tier": "bronze", "volume": 5})
        self.assertEqual(emea["outputs"]["out_price"], 18.0)

    def test_columns_that_are_neither_input_nor_output_are_ignored(self):
        table = build_table("Pricing", COLUMNS, ROWS)
        self.assertNotIn("Notes", [i["name"] for i in table["inputs"]])
        self.assertNotIn("Notes", [o["name"] for o in table["outputs"]])

    def test_every_problem_is_reported_not_just_the_first(self):
        """A 40-row sheet should not need 40 round trips to fix."""
        with self.assertRaises(ImportError_) as caught:
            build_table("Broken", COLUMNS, [
                {"Region": "APAC", "Tier": "gold", "Volume": ">=", "Price": 1, "Decision": "approve"},
                {"Region": "APAC", "Tier": "gold", "Volume": "900 - 100", "Price": 1, "Decision": "approve"},
                {"Region": "APAC", "Tier": "gold", "Volume": "1", "Price": 1, "Decision": "maybe"},
            ])
        self.assertEqual(len(caught.exception.problems), 3, caught.exception.problems)

    def test_an_unreadable_cell_refuses_rather_than_widening_the_row(self):
        """The failure direction that matters. Skipping the cell leaves no condition for that
        dimension, and a missing cell is a WILDCARD -- so a broken 'Volume' would apply this
        row's price to every volume in the matrix."""
        with self.assertRaises(ImportError_):
            build_table("Broken", COLUMNS, [
                {"Region": "APAC", "Tier": "gold", "Volume": ">=", "Price": 9.5, "Decision": "approve"}])

    def test_a_duplicate_header_is_refused(self):
        """Row dicts are keyed by header, so the second column of the same name would overwrite
        the first and one dimension of the matrix would vanish silently."""
        columns = COLUMNS + [{"header": "Region", "role": "input", "variable_id": "region2"}]
        with self.assertRaises(ImportError_) as caught:
            build_table("Dup", columns, ROWS)
        self.assertTrue(any("duplicate" in p["message"] for p in caught.exception.problems))

    def test_an_input_column_must_be_bound_to_a_variable(self):
        columns = [{"header": "Region", "role": "input"},
                   {"header": "Price", "role": "output", "type": "number"}]
        with self.assertRaises(ImportError_) as caught:
            build_table("Unbound", columns, [{"Region": "APAC", "Price": 1}])
        self.assertTrue(any("not bound" in p["message"] for p in caught.exception.problems))

    def test_a_bad_outcome_value_is_refused(self):
        with self.assertRaises(ImportError_) as caught:
            build_table("Bad", COLUMNS, [
                {"Region": "APAC", "Tier": "gold", "Volume": "1", "Price": 1, "Decision": "maybe"}])
        self.assertTrue(any("maybe" in p["message"] for p in caught.exception.problems))

    def test_a_row_with_no_output_is_refused(self):
        with self.assertRaises(ImportError_):
            build_table("Empty", COLUMNS, [{"Region": "APAC", "Tier": "gold", "Volume": "1"}])

    def test_a_matrix_needs_a_dimension_and_an_output(self):
        with self.assertRaises(ImportError_):
            build_table("None", [{"header": "Price", "role": "output"}], [{"Price": 1}])
        with self.assertRaises(ImportError_):
            build_table("None", [{"header": "R", "role": "input", "variable_id": "r"}], [{"R": "x"}])

    def test_an_unknown_hit_policy_is_refused(self):
        with self.assertRaises(ImportError_):
            build_table("Pricing", COLUMNS, ROWS, hit_policy="whatever")

    def test_the_row_limit_is_enforced(self):
        rows = [{"Region": "APAC", "Tier": "gold", "Volume": str(i), "Price": 1,
                 "Decision": "approve"} for i in range(5001)]
        with self.assertRaises(ImportError_) as caught:
            build_table("Huge", COLUMNS, rows)
        self.assertTrue(any("exceeds" in p["message"] for p in caught.exception.problems))


class _ApiBase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._orig = app_main.storage
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "dti.db"))
        restore_storage_after(self)
        self.storage = app_main.storage
        self.client = TestClient(app_main.app)
        self.tenant = self.storage.default_tenant_id
        self.key = self.storage.default_api_key or ""
        os.environ.pop("AUTH_MODE", None)

    def tearDown(self):
        self.client.close()
        # Dispose the engine, not just the tempdir: db._engine_cache is keyed by URL and never
        # evicts, so the pool keeps SQLite handles open against a directory the next line deletes.
        try:
            self.storage.engine.dispose()
        except Exception:  # pragma: no cover
            pass
        app_main.storage = self._orig
        restore_storage_after(self)
        self.tempdir.cleanup()

    def as_(self, key=None):
        return {"x-api-key": key or self.key}

    def key_for(self, role):
        return self.storage.generate_api_key_for_tenant(self.tenant, role=role)["plaintext"]

    def do_import(self, key=None, **overrides):
        body = {"name": "Pricing matrix", "columns": COLUMNS, "rows": ROWS}
        body.update(overrides)
        return self.client.post(BASE + "/import", headers=self.as_(key), json=body)


class ImportApiTest(_ApiBase):
    def test_the_import_creates_a_table_and_reports_its_gaps(self):
        """The e2eFlow end to end. The ANALYSIS is the reason to import rather than retype: the
        uncovered corners of a hand-built pricing sheet are exactly what nobody spots by reading
        it, and they surface here at import time."""
        response = self.do_import()
        self.assertEqual(response.status_code, 200, response.text)
        table = response.json()
        self.assertEqual(len(table["inputs"]), 3)
        self.assertEqual(len(table["rows"]), 3)
        self.assertIn("analysis", table)
        # This sheet genuinely has holes (APAC bronze, volumes 0-99 in APAC), so the optimiser
        # must find at least one -- an analysis that reports nothing would pass this assertion
        # vacuously if it were only `assertIn`.
        # `diagnostics` is the list; the flags summarise it. Asserting on a real gap diagnostic
        # rather than on `hasGaps`, because hasGaps is hit-policy-aware and reports only ERRORS --
        # under `first` a gap is informational, and asserting the flag would be asserting False.
        gaps = [d for d in table["analysis"]["diagnostics"] if d["type"] == "gap"]
        self.assertTrue(gaps, "the optimiser reported no gaps for a sheet that has them")

    def test_correct_column_types_are_what_make_the_gap_analysis_work(self):
        """The reason inference is not cosmetic.

        `analyze_decision_table` reasons over INTERVALS. A string-typed column cannot be ordered,
        so every row came back "not_analyzable" and the optimiser reported no gaps at all -- on
        a feature whose entire selling point is that it finds the holes in your pricing matrix.
        With types read off the data, the same sheet yields real uncovered ranges.
        """
        sheet = [
            {"Region": "APAC", "Tier": "gold", "Volume": ">= 1000", "Price": 9.5, "Decision": "approve"},
            {"Region": "APAC", "Tier": "gold", "Volume": "< 100", "Price": 18.0, "Decision": "reject"},
        ]
        table = self.do_import(name="Gappy", rows=sheet).json()
        kinds = {d["type"] for d in table["analysis"]["diagnostics"]}
        self.assertIn("gap", kinds,
                      "no gap found for a sheet that never covers volumes 100-999")
        self.assertNotIn("not_analyzable", kinds,
                         "the analyzer could not reason about a numeric column -- check field_type")

    def test_the_saved_table_is_readable_and_evaluable_afterwards(self):
        table_id = self.do_import().json()["id"]
        fetched = self.client.get("{0}/{1}".format(BASE, table_id), headers=self.as_())
        self.assertEqual(fetched.status_code, 200)
        run = self.client.post("{0}/{1}/evaluate".format(BASE, table_id), headers=self.as_(),
                               json={"variable_values": {"region": "APAC", "tier": "gold",
                                                         "volume": 5000}})
        self.assertEqual(run.status_code, 200, run.text)
        self.assertEqual(run.json()["outcome"], "approve")

    def test_a_broken_sheet_is_422_with_every_problem(self):
        response = self.do_import(rows=[
            {"Region": "APAC", "Tier": "gold", "Volume": ">=", "Price": 1, "Decision": "approve"},
            {"Region": "APAC", "Tier": "gold", "Volume": "1", "Price": 1, "Decision": "maybe"},
        ])
        self.assertEqual(response.status_code, 422, response.text)
        problems = response.json()["detail"]["problems"]
        self.assertEqual(len(problems), 2, problems)
        # Each problem names WHERE, so the author can go fix that cell rather than hunt for it.
        self.assertTrue(all("row" in p for p in problems), problems)

    def test_a_second_import_of_the_same_name_does_not_overwrite_by_default(self):
        """Two spreadsheets sharing a title is not a reason to destroy the first table, and an
        author cannot undo that."""
        first = self.do_import().json()["id"]
        second = self.do_import().json()["id"]
        self.assertNotEqual(first, second)
        self.assertEqual(len(self.storage.list_decision_tables(tenant_id=self.tenant)), 2)

    def test_replace_existing_is_opt_in_and_works(self):
        first = self.do_import().json()["id"]
        again = self.do_import(replace_existing=True, rows=ROWS[:1])
        self.assertEqual(again.status_code, 200, again.text)
        self.assertEqual(again.json()["id"], first)
        self.assertEqual(len(again.json()["rows"]), 1)


class RbacAcrossAllFiveRolesTest(_ApiBase):
    """`/api/v1/decision-tables` is an AUTHOR prefix; importing is authoring, at scale."""

    ALL_ROLES = ("owner", "admin", "policy_maker", "reviewer", "viewer")
    AUTHORS = ("owner", "admin", "policy_maker")

    def test_the_capability_is_author_and_mapped_explicitly(self):
        from app import rbac

        self.assertIn("/api/v1/decision-tables", rbac._AUTHOR_PREFIXES)
        self.assertEqual(rbac.required_capability("POST", BASE + "/import"), rbac.AUTHOR)

    def test_only_author_roles_can_import(self):
        for role in self.ALL_ROLES:
            with self.subTest(role=role):
                key = self.key if role == "owner" else self.key_for(role)
                status = self.do_import(key=key).status_code
                self.assertEqual(status, 200 if role in self.AUTHORS else 403,
                                 "{0} got {1} importing a table".format(role, status))

    def test_a_viewer_cannot_import(self):
        self.assertEqual(self.do_import(key=self.key_for("viewer")).status_code, 403)


class TenantIsolationTest(_ApiBase):
    def test_another_workspace_cannot_see_an_imported_table(self):
        table_id = self.do_import().json()["id"]
        other = self.storage.create_tenant("Other")
        other_key = self.storage.generate_api_key_for_tenant(other["id"], role="admin")["plaintext"]

        listed = self.client.get(BASE, headers=self.as_(other_key))
        self.assertEqual(listed.status_code, 200)
        self.assertNotIn(table_id, [t["id"] for t in listed.json()],
                         "another workspace can see this workspace's imported table")

        fetched = self.client.get("{0}/{1}".format(BASE, table_id), headers=self.as_(other_key))
        self.assertEqual(fetched.status_code, 404, fetched.text)


class AuditTest(_ApiBase):
    def test_the_import_is_audited_with_the_actor(self):
        """`create_decision_table` already writes a `decision_table_created` event, but with no
        actor and no way to tell a hand edit from a bulk import. An import is the one write here
        that comes from outside the product."""
        key = self.storage.generate_api_key_for_tenant(self.tenant, role="policy_maker")
        response = self.do_import(key=key["plaintext"])
        self.assertEqual(response.status_code, 200, response.text)

        events = [e for e in self.storage.list_audit_events(tenant_id=self.tenant)
                  if e.get("event_type") == "decision_table_imported"]
        self.assertEqual(len(events), 1, "the import was not audited")
        metadata = events[0].get("metadata") or {}
        self.assertEqual(metadata.get("actor_role"), "policy_maker")
        self.assertEqual(metadata.get("actor_api_key_id"), key["id"])
        self.assertEqual(metadata.get("rows"), 3)
        self.assertEqual(metadata.get("inputs"), 3)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
