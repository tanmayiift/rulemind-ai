"""Financial and health PII must not survive into the stored decision payload (Tier 6.1).

`REDACTED_KEYS` was exactly ten identity field names -- name, email, phone, telephoneNumber,
idNumber, pan, aadhaar, address, line1, line2. No card_number, no ssn, no cvv, no
account_number, no dob, no iban, no passport, no mrn. And the matcher was EXACT-KEY: it tested
`str(key).lower() in keys`, so it saw `email` but not `customer_email`, and `card_number` but
not `cardNumber`. The VALUE was never looked at.

The failure direction is that it fails SILENTLY and PERMANENTLY. A raw card number posted to
/decide was written verbatim into `payload_preview`, handed back in full to any viewer-role
user through /api/v1/audit/decisions, replicated into every backup and archive sink, and kept
for the whole retention window. Nothing errors, nothing is logged, and no amount of fixing it
later un-stores the numbers already captured -- unlike an availability bug, this one's blast
radius grows with uptime.

The repo's own QA suite already knew: qa/e2e-full/suite_core.py case 6.6 posted the Luhn-valid
test card 4111111111111111, found it echoed straight back, and called `c.warn(...)`. It is a
`c.fail(...)` now, and these tests are the unit-level version of the same assertion.

Two distinct defences, tested separately below because they fail independently:

  * KEY matching, now normalised (card_number == cardNumber == "Card Number") and anchored at
    the trailing tokens (customer_email, applicantCardNumber).
  * VALUE-SHAPE detection, which did not exist anywhere in this repo. Key matching cannot see
    a card number sent as `external_ref`, and the caller picks the field names, not us.

Both are deliberately conservative. Over-redaction is not a safe default either: a false
positive silently destroys data the customer needs and the original is not recoverable from
the decision log. Every value detector therefore carries a checksum (Luhn, IBAN mod-97) or a
fixed separator layout, and the negative tests below are as load-bearing as the positive ones.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
os.environ.setdefault("RULEMIND_SEED_DEMO", "1")
os.environ.pop("AUTH_MODE", None)

from fastapi.testclient import TestClient  # noqa: E402

import app.main as app_main  # noqa: E402
from app.logic import (  # noqa: E402
    REDACTED_KEYS,
    looks_like_card_number,
    looks_like_iban,
    looks_like_ssn,
    redact_payload,
)
from app.storage import Storage  # noqa: E402

# Luhn-valid, never issued: the industry's standard test numbers.
TEST_VISA = "4111111111111111"
TEST_MASTERCARD = "5500005555555559"
TEST_IBAN = "GB82WEST12345698765432"


class RedactedKeyDefaultsTests(unittest.TestCase):
    """The defaults themselves. Each of these names was absent before."""

    def test_financial_and_health_families_are_defaults(self):
        for key in ("card_number", "cvv", "account_number", "iban", "ssn",
                    "passport", "mrn", "dob"):
            self.assertIn(key, REDACTED_KEYS, "{0} must redact without configuration".format(key))

    def test_card_number_no_longer_reaches_the_stored_payload(self):
        redacted = redact_payload({"card_number": TEST_VISA, "amount": 1200})
        self.assertEqual(redacted["card_number"], "***")
        self.assertEqual(redacted["amount"], 1200, "non-PII fields must survive intact")

    def test_key_matching_is_punctuation_and_case_insensitive(self):
        # `cardNumber` did not match `card_number` under the old exact-key comparison.
        redacted = redact_payload({"cardNumber": TEST_VISA, "CARD-NUMBER": TEST_VISA,
                                   "Card Number": TEST_VISA})
        self.assertEqual(set(redacted.values()), {"***"})

    def test_trailing_token_match_covers_qualified_names(self):
        redacted = redact_payload({
            "customer_email": "a@b.com",
            "applicantCardNumber": TEST_MASTERCARD,
            "primary_account_number": "12345678",
            "borrower_dob": "1988-04-02",
        })
        self.assertEqual(sorted(set(redacted.values())), ["***"])

    def test_lookalike_business_fields_are_not_over_redacted(self):
        """The other failure mode. `account_id` is not `account_number`, and `policy_name` is a
        decisioning artifact this product is built around -- blanking either would corrupt the
        audit trail in the name of protecting it."""
        payload = {
            "account_id": "acc_98765",
            "policy_name": "Prime Personal Loan",
            "rule_name": "age_gate",
            "product_name": "Instant Loan",
            "loan_amount": 250000,
            "credit_score": 720,
        }
        self.assertEqual(redact_payload(payload), payload)

    def test_operator_configured_keys_also_get_the_qualified_treatment(self):
        redacted = redact_payload({"account_no": "1", "customer_account_no": "2", "notes": "ok"},
                                  extra_keys=["account_no"])
        self.assertEqual(redacted["account_no"], "***")
        self.assertEqual(redacted["customer_account_no"], "***")
        self.assertEqual(redacted["notes"], "ok")

    def test_exact_mode_is_an_escape_hatch_from_the_token_walk(self):
        with mock.patch.dict(os.environ, {"RULEMIND_PII_KEY_MATCH": "exact"}):
            redacted = redact_payload({"customer_email": "a@b.com", "email": "a@b.com"})
        self.assertEqual(redacted["customer_email"], "a@b.com")
        self.assertEqual(redacted["email"], "***", "whole-key matches still apply in exact mode")


class ValueShapeDetectionTests(unittest.TestCase):
    """Detection that ignores the key entirely. None of this existed before."""

    def test_card_number_under_an_innocuous_key_is_caught(self):
        # The case key-name matching structurally cannot cover, and the realistic one: nobody
        # exfiltrates a card by naming the field `card_number`.
        redacted = redact_payload({"external_ref": TEST_VISA})
        self.assertEqual(redacted["external_ref"], "***")

    def test_detection_recurses_into_lists_and_nested_objects(self):
        redacted = redact_payload({"applicants": [{"meta": {"token": TEST_MASTERCARD}}]})
        self.assertEqual(redacted["applicants"][0]["meta"]["token"], "***")

    def test_epoch_timestamps_are_not_mistaken_for_cards(self):
        """The false positive that requiring an issuer prefix exists to prevent.

        A 13-digit millisecond epoch and a 19-digit nanosecond epoch are both inside the card
        length range, and one in ten of them passes Luhn by chance. Both begin "17", which is
        not an issuer identification number, so neither can be blanked no matter how the check
        digit lands.
        """
        payload = {"created_ms": "1756900000000", "created_ns": "1756900000000000000",
                   "created_int": 1756900000000}
        self.assertEqual(redact_payload(payload), payload)

    def test_a_bare_nine_digit_number_is_not_treated_as_an_ssn(self):
        """Nine digits is equally an account number, an order id or a customer reference.
        Blanking those would be a worse bug than the one this module fixes, so only the
        separated layout counts."""
        payload = {"reference": "123456789", "order_no": "987654321"}
        self.assertEqual(redact_payload(payload), payload)

    def test_separated_ssn_and_checksum_valid_iban_are_caught(self):
        redacted = redact_payload({"note": "123-45-6789", "beneficiary": TEST_IBAN})
        self.assertEqual(redacted["note"], "***")
        self.assertEqual(redacted["beneficiary"], "***")

    def test_detectors_reject_near_misses(self):
        self.assertTrue(looks_like_card_number(TEST_VISA))
        self.assertFalse(looks_like_card_number(TEST_VISA[:-1] + "2"), "must fail Luhn")
        self.assertFalse(looks_like_card_number("1756900000000"), "no issuer prefix")
        self.assertTrue(looks_like_ssn("123-45-6789"))
        self.assertFalse(looks_like_ssn("000-45-6789"), "000 area is never issued")
        self.assertFalse(looks_like_ssn("123456789"), "unseparated is too ambiguous")
        self.assertTrue(looks_like_iban(TEST_IBAN))
        self.assertFalse(looks_like_iban(TEST_IBAN[:-1] + "3"), "must fail mod-97")

    def test_aggressiveness_is_configurable_in_both_directions(self):
        with mock.patch.dict(os.environ, {"RULEMIND_PII_VALUE_SCAN": "off"}):
            self.assertEqual(redact_payload({"external_ref": TEST_VISA})["external_ref"], TEST_VISA)
        with mock.patch.dict(os.environ, {"RULEMIND_PII_VALUE_SCAN": "aggressive"}):
            # Numeric values are scanned only here: without separators or an issuer prefix to
            # check, a long integer is far more often an id than a card.
            self.assertEqual(redact_payload({"ref": int(TEST_VISA)})["ref"], "***")
        self.assertEqual(redact_payload({"ref": int(TEST_VISA)})["ref"], int(TEST_VISA))


class DecisionLogLeakTests(unittest.TestCase):
    """The end-to-end path the QA suite exercises: POST /decide, then read it back."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self._orig_storage = app_main.storage
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "pii.db"))
        self.client = TestClient(app_main.app)
        self.headers = {"x-api-key": app_main.storage.default_api_key or ""}
        self.policy_id = self.client.get("/api/v1/policies", headers=self.headers).json()[0]["id"]

    def tearDown(self):
        self.client.close()
        self.tempdir.cleanup()

        # Restore the global, and release this test's engine. Placed at the END of tearDown so
        # the teardown above still runs against THIS test's storage -- an earlier version put it
        # first and silently redirected those statements at the restored global.
        #
        # The restore is the load-bearing half. Leaving app_main.storage pointed at a deleted
        # tempdir made an UNRELATED module later fail with
        # `sqlite3.OperationalError: unable to open database file`: the failure lands on whoever
        # reads the global next, never on the test that leaked it, which is why it looked like a
        # random flake for so long.
        try:
            app_main.storage.engine.dispose()
        except Exception:  # pragma: no cover - teardown must not fail the test
            pass
        try:
            app_main.storage = self._orig_storage
        except Exception:  # pragma: no cover
            pass

    def test_a_card_number_is_never_echoed_back_from_the_audit_trail(self):
        posted = self.client.post("/api/v1/decide", headers=self.headers, json={
            "policyId": self.policy_id,
            "payload": {"age": 30, "card_number": TEST_VISA, "external_ref": TEST_VISA},
            "userId": "pii-regression",
        })
        self.assertEqual(posted.status_code, 200, posted.text)
        listed = self.client.get("/api/v1/audit/decisions", headers=self.headers, params={"limit": 20})
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertNotIn(TEST_VISA, listed.text,
                         "the stored payload_preview still contains a raw card number")


if __name__ == "__main__":
    unittest.main()
