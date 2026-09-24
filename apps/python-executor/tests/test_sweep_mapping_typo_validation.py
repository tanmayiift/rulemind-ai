"""A mistyped JSONPath in a transform mapping must be refused at AUTHORING time.

`_execute_transform` tests `source.startswith("$.")`. A mapping written `"$variables.score"` --
one missing dot -- fails that test, falls through to the LITERAL branch, and the STRING
"$variables.score" is written into the transform output. A downstream rule then compares against
that text. Nothing raises, nothing is logged, and the decision is quietly wrong.

Author time is the only cheap place to catch it: by decision time the value is a perfectly valid
literal and there is nothing for the executor to object to.

The validation is deliberately NARROW, and the tests that matter most here are the negative ones.
A leading `$` is not evidence of a typo -- "$100" and "$USD" are literals someone may genuinely
want in an output field, and refusing them would be a worse bug than the one being fixed. Only
`$` + identifier + `.` is unambiguous.
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

from fastapi import HTTPException  # noqa: E402

import app.main as app_main  # noqa: E402
from app.context import reset_current_tenant_id, set_current_tenant_id  # noqa: E402
from app.storage import Storage  # noqa: E402
from tests._storage_global import preserve_storage_global  # noqa: E402


def _transform(mapping):
    return {"type": "transform", "id": "tx", "config": {"outputKey": "computed", "mapping": mapping}}


class MappingTypoValidationTests(unittest.TestCase):
    """Isolated storage and an explicit tenant context.

    This file used to have no setUp at all: it called `validate_policy_steps` as a plain
    function against whatever `app_main.storage` the previous test module happened to leave
    behind. That worked only because `Storage._tenant_id` fell back to the FIRST tenant in the
    table when nothing else resolved -- so on a shared storage with several tenants it read
    someone else's rules and did not care, which is the disclosure that fallback was closed for.
    Closing it turned a silent dependency on global state into 7 loud errors, which is the
    right outcome and is why the fix was worth making.

    Same pattern as test_enterprise: these tests call route handlers as plain Python functions,
    so the tenant middleware never runs; setUp establishes the context the middleware would
    have set.
    """

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tempdir.cleanup)
        preserve_storage_global(self)
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "typo.db"))
        token = set_current_tenant_id(app_main.storage.default_tenant_id)
        self.addCleanup(reset_current_tenant_id, token)

    def _validate(self, step):
        app_main.validate_policy_steps([step, {"type": "outcome", "ref_id": "approve"}])

    def test_a_missing_dot_after_the_dollar_is_refused(self):
        with self.assertRaises(HTTPException) as caught:
            self._validate(_transform({"score": "$variables.bureau_score"}))
        self.assertEqual(caught.exception.status_code, 422)
        detail = str(caught.exception.detail)
        self.assertIn("$variables.bureau_score", detail)
        self.assertIn("$.variables.bureau_score", detail,
                      "the message must name the correction, not just the problem")

    def test_a_model_input_mapping_is_checked_too(self):
        step = {"type": "model", "id": "m", "ref_id": "m1",
                "config": {"inputMapping": {"a": "$payload.amount"}, "outputVariable": "risk"}}
        with self.assertRaises(HTTPException):
            self._validate(step)

    def test_the_expr_form_is_checked(self):
        with self.assertRaises(HTTPException):
            self._validate(_transform({"score": {"expr": "$variables.a + 1"}}))

    # ---- the negatives, which are the point of making this narrow ----------------------------

    def test_a_correct_path_is_accepted(self):
        self._validate(_transform({"score": "$.variables.bureau_score"}))

    def test_a_currency_literal_is_not_a_typo(self):
        """"$100" starts with a dollar and is an ordinary literal. Refusing it would be a worse
        bug than the one this validation exists for."""
        self._validate(_transform({"fee": "$100", "ccy": "$USD", "note": "$ 5.00"}))

    def test_a_plain_literal_is_accepted(self):
        self._validate(_transform({"tier": "gold", "n": 5, "flag": True}))

    def test_a_step_with_no_mapping_is_accepted(self):
        self._validate({"type": "transform", "id": "tx", "config": {"outputKey": "computed"}})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
