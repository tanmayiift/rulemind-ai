"""One undecryptable row must not take out the whole decision log.

WHAT HAPPENED. A row in `decisions` was encrypted under a different RULEMIND_CONFIG_KEY than the
API was running with -- the ordinary result of a key rotation, a restore, or (here) a test lane
that shared the app's database. `decrypt_decision_field` raises on that, by design, and every
listing went through it, so `GET /api/v1/audit/decisions` returned **HTTP 500** and took
/audit, /decision-explorer and /reports down together.

The browser reported it as a CORS error, because an unhandled 500 never reaches the CORS
middleware that would have attached the header. So the visible symptom named the wrong
subsystem, and the real cause existed only in a server-side traceback.

THE DISTINCTION THIS FILE PINS. Strict is right for ONE decision: an audit record you cannot
decrypt must not be handed back as though it were fine. Lenient is right for a LIST: 11,800
readable rows hidden by one unreadable row is the worst available trade, and it hides the
problem at exactly the moment the operator needs to see it.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")

from app import storage as storage_mod  # noqa: E402


class DecisionListingResilienceTest(unittest.TestCase):
    # A valid Fernet token this process's key cannot open -- which is exactly what a row written
    # under a rotated key looks like. Generated from a DIFFERENT key rather than by corrupting
    # bytes, so it reaches the InvalidToken path rather than a base64 error.
    def _foreign_token(self) -> str:
        from cryptography.fernet import Fernet
        return Fernet(Fernet.generate_key()).encrypt(b'{"score": 710}').decode("utf-8")

    def test_strict_decrypt_still_refuses(self):
        """The single-record contract is unchanged: it must not return ciphertext as plaintext."""
        with self.assertRaises(storage_mod.DecryptionError):
            storage_mod.decrypt_decision_field(self._foreign_token())

    def test_lenient_decrypt_returns_the_callers_own_zero_value(self):
        """THE SHAPE MUST SURVIVE, and the first version of this fix broke exactly that.

        It returned a marker DICT for every field. `trace` and `rule_results` are LISTS, and a
        non-empty dict is truthy, so it defeated the `or []` guards at the call site too. The
        dashboard died on `(selected.trace ?? []).map is not a function` -- the guard was for
        null and it had been handed an object. A fallback that changes a field's type is not a
        fallback; it just moves the crash.
        """
        token = self._foreign_token()
        self.assertEqual(storage_mod.decrypt_decision_field_lenient(token, []), [])
        self.assertEqual(storage_mod.decrypt_decision_field_lenient(token, {}), {})
        self.assertIsNone(storage_mod.decrypt_decision_field_lenient(token, None))

    def test_the_zero_value_is_a_copy_not_the_callers_object(self):
        """Callers deepcopy into response bodies; handing back the same object would let one
        response mutate the default for every later one."""
        token = self._foreign_token()
        shared: list = []
        a = storage_mod.decrypt_decision_field_lenient(token, shared)
        a.append("mutated")
        self.assertEqual(shared, [], "the caller's own default was mutated")
        self.assertEqual(storage_mod.decrypt_decision_field_lenient(token, shared), [])

    def test_an_unreadable_row_is_flagged_rather_than_silently_empty(self):
        """Empty and unreadable must be distinguishable, or the operator cannot tell a decision
        that stored nothing from one whose key is gone."""
        self.assertTrue(storage_mod.decision_field_is_undecryptable(self._foreign_token()))
        self.assertFalse(storage_mod.decision_field_is_undecryptable(
            storage_mod.encrypt_decision_field({"score": 710})))
        self.assertFalse(storage_mod.decision_field_is_undecryptable(None))

    def test_readable_and_legacy_values_are_untouched(self):
        """Leniency must not change what a GOOD row returns -- otherwise the fix for the 500 is a
        silent change to every decision in the log."""
        encrypted = storage_mod.encrypt_decision_field({"score": 710})
        self.assertEqual(storage_mod.decrypt_decision_field_lenient(encrypted), {"score": 710})
        # Pre-encryption plaintext, and a JSON column that round-tripped as a string.
        self.assertEqual(storage_mod.decrypt_decision_field_lenient({"a": 1}), {"a": 1})
        self.assertEqual(storage_mod.decrypt_decision_field_lenient('{"a": 1}'), {"a": 1})
        self.assertIsNone(storage_mod.decrypt_decision_field_lenient(None))


if __name__ == "__main__":
    unittest.main()
