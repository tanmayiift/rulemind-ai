"""A nested input key that is wrong must not silently change the decision (parity P06).

The decision payload is source-keyed and deeply nested. Nothing checked the nested keys, so a
caller could misspell one, send a string where a number belongs, or drop a required leaf, and get
HTTP 200 and a decision. The bad key simply resolved to `None`.

`None` is the worst possible substitute, because `logic.py` returns False for EVERY comparison
against it. So a typo does not fail -- it re-phrases the rule, and the DIRECTION it breaks is
decided by how the author happened to word the gate:

    "reject if avg_balance < 10000"   -> None < 10000 is False -> NOT rejected   (fails OPEN)
    "approve if avg_balance >= 10000" -> None >= 10000 is False -> not approved  (fails closed)

Every "flag the bad ones" rule -- the whole fraud and thin-file category -- failed open on a typo.
This is the same failure mode Tier 1.2 fixed for a variable that FAILED to compute, one step
earlier in the pipeline: there the input errored and the error was discarded, here the input was
never checked in the first place.

`SilentTypoDirectionTest` at the bottom is the concrete bug, reproduced: one character changed in
one nested key turns a 500-rupee balance into an approval.

WHY IT IS OPT-IN. A hard schema check switched on globally would refuse every integration already
in production on its first request, so it is off by default and enabled per tenant or per policy
under `engine_config.input_validation`. `OffByDefaultTest` is the guard on that promise.
"""
from __future__ import annotations

import copy
import os
import sys
import tempfile
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
os.environ.setdefault("RULEMIND_SEED_DEMO", "1")  # the seeded `bank` source declares a nested schema
os.environ.pop("AUTH_MODE", None)

from fastapi.testclient import TestClient  # noqa: E402

import app.main as app_main  # noqa: E402
from app import fast_decide, input_schema  # noqa: E402
from app.storage import Storage  # noqa: E402

# The seeded `bank` connector declares: summary.avgBalance, salary[].amount, bounces[].amount,
# emiPayments[].amount -- a genuinely nested schema with arrays, which is the shape that had no
# validation at all.
BANK_COMPLETE = {
    "summary": {"avgBalance": 45200},
    "salary": [{"amount": 62000}],
    "bounces": [{"amount": 5000}],
    "emiPayments": [{"amount": 8500}],
}

BALANCE_VARIABLE = {
    "id": "bank_balance",
    "name": "Average Bank Balance",
    "category": "Bank",
    "source_id": "bank",
    "description": "Average balance from the statement summary",
    # Returns None -- not 0 -- when the key is absent. That is the honest reading of a missing
    # key, and it is exactly what makes the typo invisible downstream.
    "code": (
        '@variable(source="bank")\n'
        "def bank_balance(payload, variables, apis):\n"
        '    return payload.get("summary", {}).get("avgBalance")\n'
    ),
    "status": "prod",
}

# Phrased as a catch: the DANGEROUS outcome sits on the pass branch, which is how every
# "flag the bad ones" rule is written.
THIN_FILE_RULE = {
    "id": "rule_thin_file",
    "name": "Thin File Gate",
    "nodes": [],
    "tree": {
        "type": "group", "id": "root", "logic": "AND",
        "children": [{"type": "condition", "id": "c1",
                      "variable": "bank_balance", "operator": "<", "value": 10000}],
        "onPass": "reject",
        "onFail": "approve",
    },
    "rule_format": "v1",
    "expression": "IF (Average Bank Balance < 10000) -> REJECT ELSE -> APPROVE",
    "status": "prod",
}

BANK_POLICY = {
    "id": "pol_bank",
    "name": "Bank Policy",
    "defaultOutcome": "review",
    "steps": [{"id": "s1", "type": "rule", "ref_id": "rule_thin_file", "label": "Thin File Gate"}],
}


def bank_payload(**overrides):
    """A COMPLETE bank source payload, so any issue the test asserts is the one it introduced.

    The source-keyed form is authoritative (see the /decide docstring): fields you omit are
    absent, not defaulted. So a deliberately partial payload would produce a pile of
    missing_required issues that drown the one under test.
    """
    payload = copy.deepcopy(BANK_COMPLETE)
    payload.update(overrides)
    return {"bank": payload}


class _Base(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._orig_storage = app_main.storage
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "inputval.db"))
        self.tenant = app_main.storage.default_tenant_id
        app_main.storage.create_variable(dict(BALANCE_VARIABLE), tenant_id=self.tenant)
        app_main.storage.create_rule(dict(THIN_FILE_RULE), tenant_id=self.tenant)
        app_main.storage.create_policy(dict(BANK_POLICY), tenant_id=self.tenant)
        self.client = TestClient(app_main.app)
        self.headers = {"x-api-key": app_main.storage.default_api_key or ""}

    def tearDown(self):
        self.client.close()
        self.tempdir.cleanup()
        fast_decide.invalidate()

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

    def configure(self, config):
        """Write the tenant's input_validation block.

        The catalog the executor reads is served from fast_decide's cross-request cache with a
        5s TTL backstop, and update_settings does not bump the epoch -- so a test that changed
        settings and decided immediately could read the previous block. Invalidating makes the
        change take effect now rather than eventually.
        """
        settings = app_main.storage.get_settings(tenant_id=self.tenant)
        engine_config = dict(settings.get("engine_config") or {})
        engine_config["input_validation"] = config
        app_main.storage.update_settings({"engine_config": engine_config}, tenant_id=self.tenant)
        fast_decide.invalidate()

    def decide(self, payload, policy="pol_bank"):
        return self.client.post("/api/v1/decide", headers=self.headers,
                                json={"policyId": policy, "payload": payload})

    @staticmethod
    def issues(response):
        return response.json()["detail"]["issues"]

    @staticmethod
    def paths(response):
        return [issue["path"] for issue in _Base.issues(response)]


class OffByDefaultTest(_Base):
    """The compatibility guard. Every integration in production today sends payloads nobody ever
    validated; switching a schema check on globally would 422 all of them at once."""

    def test_a_misspelled_nested_key_still_decides(self):
        response = self.decide(bank_payload(summary={"avgBalanace": 500}))
        self.assertEqual(response.status_code, 200, response.text[:300])

    def test_nothing_is_added_to_the_trace(self):
        body = self.decide(bank_payload(summary={"avgBalanace": 500})).json()
        self.assertEqual([s for s in (body.get("trace") or [])
                          if s.get("type") == "input_validation"], [],
                         "validation ran on a workspace that never enabled it")

    def test_an_empty_or_absent_block_is_off_not_on(self):
        self.assertFalse(input_schema.resolve_settings({}, "pol_bank").enabled)
        self.assertFalse(input_schema.resolve_settings({"input_validation": {}}, "pol_bank").enabled)


class EnforceTest(_Base):
    def setUp(self):
        super().setUp()
        self.configure({"mode": "enforce"})

    def test_a_valid_payload_is_unaffected(self):
        """The regression guard. Rejecting bad input is only correct if good input still runs."""
        response = self.decide(bank_payload())
        self.assertEqual(response.status_code, 200, response.text[:300])
        self.assertIn(response.json()["outcome"], ("approve", "reject", "review"))

    def test_a_misspelled_nested_key_is_rejected(self):
        response = self.decide(bank_payload(summary={"avgBalanace": 500}))
        self.assertEqual(response.status_code, 422, response.text[:300])
        self.assertEqual(response.json()["detail"]["error"], "invalid_input",
                         "a caller's typo is a 422, not a 500 that pages on-call")

    def test_the_error_names_the_full_nested_path(self):
        """The parity row's e2eFlow verbatim: a nested payload with a bad leaf -> INVALID_INPUT
        names the PATH. Naming the source, or the flat field it maps to, is not enough to find
        one wrong key in a bureau pull."""
        response = self.decide(bank_payload(summary={"avgBalanace": 500}))
        self.assertIn("bank.summary.avgBalance", self.paths(response))
        self.assertIn("bank.summary.avgBalanace", self.paths(response))

    def test_a_bad_key_inside_an_array_is_named_with_its_index(self):
        response = self.decide(bank_payload(salary=[{"amount": 62000}, {"amount": "62000"}]))
        self.assertIn("bank.salary[1].amount", self.paths(response),
                      "an issue in the second array element must say which element")

    def test_every_problem_is_reported_at_once(self):
        """A caller fixing one field per round trip is a bad API -- and worse here, because the
        payload shapes are big enough that serial discovery is measured in dozens of requests."""
        response = self.decide({"bank": {"summary": {"avgBalance": "45200"},
                                         "salary": [{"amount": 62000}],
                                         "bouncees": [{"amount": 5000}]}})
        paths = self.paths(response)
        self.assertIn("bank.summary.avgBalance", paths)      # wrong type
        self.assertIn("bank.bouncees", paths)                # typo'd key
        self.assertIn("bank.bounces", paths)                 # ...and the required one it displaced
        self.assertIn("bank.emiPayments", paths)             # a second missing required
        self.assertGreaterEqual(len(paths), 4)

    def test_non_blocking_problems_are_reported_too_and_marked(self):
        """The unknown key does not block under the default `unknown_keys: warn`, but a caller
        who fixes only the blockers and is THEN rejected for a key nobody mentioned has been sent
        round the loop for nothing."""
        response = self.decide({"bank": {"summary": {"avgBalance": "45200"},
                                         "salary": [{"amount": 62000}],
                                         "bouncees": [{"amount": 5000}]}})
        by_path = {issue["path"]: issue for issue in self.issues(response)}
        self.assertFalse(by_path["bank.bouncees"]["blocking"])
        self.assertTrue(by_path["bank.summary.avgBalance"]["blocking"])

    def test_the_three_kinds_are_distinguished(self):
        """An unknown key is probably a typo and an operator may choose to tolerate it. A wrong
        type and a missing required leaf are not opinions -- both mean a gate reads None."""
        response = self.decide({"bank": {"summary": {"avgBalance": "45200"},
                                         "salary": [{"amount": 62000}],
                                         "bouncees": [{"amount": 5000}]}})
        kinds = {issue["kind"] for issue in self.issues(response)}
        self.assertEqual(kinds, {"unknown_key", "wrong_type", "missing_required"})

    def test_a_boolean_is_not_accepted_where_a_number_belongs(self):
        """`isinstance(True, int)` is True in Python, so a naive numeric check waves this
        through -- and True then scores as 1."""
        response = self.decide(bank_payload(summary={"avgBalance": True}))
        self.assertEqual(response.status_code, 422)
        self.assertEqual([i["kind"] for i in self.issues(response) if i["path"] == "bank.summary.avgBalance"],
                         ["wrong_type"])

    def test_an_explicit_null_on_a_required_field_is_rejected(self):
        """The spelling is right, so nothing else would ever notice -- and the gate still reads
        None, which is the entire problem."""
        response = self.decide(bank_payload(summary={"avgBalance": None}))
        self.assertEqual(response.status_code, 422)
        self.assertIn("bank.summary.avgBalance", self.paths(response))

    def test_a_rejected_payload_produces_no_decision(self):
        """Fail closed, not fail soft. A rejected payload must not leave an outcome, a Decision
        row, or a billable unit behind -- 'validated' and 'decided anyway' is the worst of both."""
        from app import decision_log

        self.decide(bank_payload(summary={"avgBalanace": 500}))
        decision_log.flush()
        rows = app_main.storage.list_decisions(tenant_id=self.tenant)
        self.assertEqual([r for r in rows if r.get("policy_id") == "pol_bank"], [],
                         "a payload that failed validation still produced a decision")

    def test_the_rejection_is_alertable(self):
        """Recorded as an error event, so a broken integration is visible without reading 422s
        out of an access log."""
        self.decide(bank_payload(summary={"avgBalanace": 500}))
        events = app_main.storage.list_error_events(tenant_id=self.tenant)
        self.assertTrue([e for e in events if e.get("stage") == "input_validation"],
                        "a rejected payload produced no error event")

    def test_the_flat_form_is_not_required_checked(self):
        """The two payload forms have distinct, intentional semantics (see
        tests/test_decide_payload_forms.py). A flat field is an OVERRIDE on top of each source's
        sample, so nothing it omits is 'missing' -- validating it as if it were authoritative
        would reject the quick what-if form the console and the SDK manifests rely on."""
        response = self.decide({"bureau_score": 800})
        self.assertEqual(response.status_code, 200, response.text[:300])

    def test_a_misspelled_flat_field_is_still_caught(self):
        """It reaches no source's variables at all, so today it is silently inert."""
        self.configure({"mode": "enforce", "unknown_keys": "reject"})
        response = self.decide({"bureu_score": 800})
        self.assertEqual(response.status_code, 422)
        self.assertIn("bureu_score", self.paths(response))

    def test_a_source_with_no_declared_schema_is_skipped(self):
        """The seeded `custom` source is 'any JSON' by design. Inventing a schema for it would
        reject exactly the payloads it exists to accept."""
        self.assertIsNone(input_schema.build_source_schema(
            {"schema_paths": ["(user-defined)"], "sample_payload": {}}))


class UnknownKeyPolicyTest(_Base):
    """An unknown key is the one kind an operator can reasonably downgrade: it is far likelier to
    be a harmless extra field a client sends than a typo. A wrong type and a missing required
    leaf are not tunable, because both mean a gate reads None."""

    def test_an_unknown_key_alone_warns_rather_than_blocks_by_default(self):
        self.configure({"mode": "enforce"})
        response = self.decide(bank_payload(extraField=1))
        self.assertEqual(response.status_code, 200, response.text[:300])
        trace = [s for s in response.json()["trace"] if s.get("type") == "input_validation"]
        self.assertEqual(len(trace), 1, "the unknown key was not reported at all")
        self.assertEqual(trace[0]["status"], "warned")

    def test_it_blocks_when_configured_to_reject(self):
        self.configure({"mode": "enforce", "unknown_keys": "reject"})
        response = self.decide(bank_payload(extraField=1))
        self.assertEqual(response.status_code, 422)
        self.assertIn("bank.extraField", self.paths(response))

    def test_ignore_suppresses_it_entirely(self):
        self.configure({"mode": "enforce", "unknown_keys": "ignore"})
        response = self.decide(bank_payload(extraField=1))
        self.assertEqual(response.status_code, 200)
        self.assertEqual([s for s in response.json()["trace"] if s.get("type") == "input_validation"], [])

    def test_ignoring_unknown_keys_does_not_soften_a_wrong_type(self):
        self.configure({"mode": "enforce", "unknown_keys": "ignore"})
        response = self.decide(bank_payload(summary={"avgBalance": "45200"}, extraField=1))
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.paths(response), ["bank.summary.avgBalance"])


class WarnModeTest(_Base):
    """The migration step. An operator has to be able to see what enforcement WOULD reject
    before switching it on, or nobody ever switches it on."""

    def setUp(self):
        super().setUp()
        self.configure({"mode": "warn"})

    def test_the_decision_proceeds(self):
        response = self.decide(bank_payload(summary={"avgBalanace": 500}))
        self.assertEqual(response.status_code, 200, response.text[:300])

    def test_but_the_problems_are_in_the_trace(self):
        body = self.decide(bank_payload(summary={"avgBalanace": 500})).json()
        trace = [s for s in body["trace"] if s.get("type") == "input_validation"]
        self.assertEqual(len(trace), 1)
        self.assertEqual(trace[0]["status"], "warned")
        self.assertIn("bank.summary.avgBalance", [i["path"] for i in trace[0]["issues"]])

    def test_and_recorded_as_an_error_event(self):
        self.decide(bank_payload(summary={"avgBalanace": 500}))
        events = [e for e in app_main.storage.list_error_events(tenant_id=self.tenant)
                  if e.get("stage") == "input_validation"]
        self.assertTrue(events, "warn mode is meant to be watched and recorded nothing")


class ConfigResolutionTest(_Base):
    def test_a_policy_can_be_enforced_without_enforcing_the_workspace(self):
        """One strict integration should not require switching every other policy on."""
        self.configure({"mode": "off", "policies": {"pol_bank": {"mode": "enforce"}}})
        self.assertEqual(self.decide(bank_payload(summary={"avgBalanace": 500})).status_code, 422)
        # A different policy in the same workspace still decides.
        self.assertEqual(self.decide({"age": 30}, policy="policy_instant_personal_loan").status_code, 200)

    def test_a_policy_can_be_exempted_from_an_enforced_workspace(self):
        self.configure({"mode": "enforce", "policies": {"pol_bank": {"mode": "off"}}})
        self.assertEqual(self.decide(bank_payload(summary={"avgBalanace": 500})).status_code, 200)

    def test_a_typo_in_the_mode_does_not_silently_disable_validation(self):
        """The config is the last place to reproduce the fail-open this feature exists to close.
        Absent means off; PRESENT but unreadable means the operator wanted validation."""
        self.assertEqual(input_schema.resolve_settings({"input_validation": {"mode": "enfroce"}}, "p").mode,
                         "enforce")
        self.assertEqual(input_schema.resolve_settings({"input_validation": {"mode": "off"}}, "p").mode,
                         "off")

    def test_the_shorthand_forms_work(self):
        self.assertTrue(input_schema.resolve_settings({"input_validation": True}, "p").enabled)
        self.assertFalse(input_schema.resolve_settings({"input_validation": False}, "p").enabled)
        self.assertEqual(input_schema.resolve_settings({"input_validation": "warn"}, "p").mode, "warn")


class SilentTypoDirectionTest(_Base):
    """THE BUG, reproduced. One character, one nested key, opposite decision.

    `bank_balance` reads `payload["summary"]["avgBalance"]`. The rule is phrased as a catch --
    "reject if balance < 10000" -- which is how every thin-file and fraud rule is written, and
    which is the phrasing that fails OPEN: `None < 10000` is False, so the rule takes its onFail
    branch, which for a catch-shaped rule is the permissive one.

    So an applicant with 500 rupees is APPROVED, at HTTP 200, with nothing in the response or the
    logs to distinguish it from a clean approval.
    """

    def test_the_real_balance_is_rejected(self):
        """Baseline: spelled correctly, the gate works."""
        body = self.decide(bank_payload(summary={"avgBalance": 500})).json()
        self.assertEqual(body["outcome"], "reject")

    def test_one_transposed_character_approves_the_same_applicant(self):
        """Unfixed behaviour, pinned deliberately: with validation off this is what happens."""
        body = self.decide(bank_payload(summary={"avgBalanace": 500})).json()
        self.assertEqual(body["outcome"], "approve",
                         "if this ever stops approving, the typo stopped being invisible and "
                         "this whole feature can be re-scoped")
        self.assertIsNone(body["variables"]["bank_balance"])
        self.assertEqual(body["variable_errors"], {},
                         "nothing errored -- which is exactly why nobody noticed")

    def test_validation_turns_that_approval_into_a_refusal(self):
        """The fix. Not a different outcome -- no outcome at all, because the input was never
        good enough to decide on."""
        self.configure({"mode": "enforce"})
        response = self.decide(bank_payload(summary={"avgBalanace": 500}))
        self.assertEqual(response.status_code, 422)
        self.assertIn("bank.summary.avgBalance", self.paths(response))
        self.assertNotIn("outcome", response.json(),
                         "a refused payload must not carry a decision outcome")


if __name__ == "__main__":
    unittest.main()
