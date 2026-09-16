"""The `transform` step: JSONPath mapping, output keying, and reachability.

WHY THIS FILE EXISTS. `transform` is implemented at executor.py:1672 and reads two config
fields -- `config.mapping` (a target-field -> JSONPath map) and `config.outputKey`. Before
this module, a repo-wide grep for `outputKey` returned FOUR hits and not one of them was a
test: executor.py itself, and three web/authoring UI files. The step type appears in the
Python suites only inside fast-path-eligibility assertions (`_IO_STEP_TYPES` contains
"transform", so a policy holding one is not fast-servable) -- i.e. tests that assert the step
is NOT executed. A bug in the JSONPath mapping was caught by nothing at all.

WHAT THE MAPPING ACTUALLY DOES, per value shape (executor.py:1677-1683):
    "$...."            -> resolve_jsonpath against the context view
    {"expr": "..."}    -> evaluate_math_expr (a restricted AST evaluator)
    anything else      -> stored VERBATIM as a literal

The literal fallback is intentional (a mapping may pin a constant), but it is also the reason
a mistyped path fails silently rather than loudly -- see
test_a_path_missing_its_dollar_prefix_is_stored_as_a_literal_string.

The `@unittest.expectedFailure` tests below are PROVEN DEFECTS that are not fixed here. Each
docstring states what the correct behaviour is and where the fix belongs. They are expected
failures rather than assertions of today's behaviour deliberately: pinning a bug as "correct"
is how it becomes permanent, and an unexpected success is a loud signal that someone fixed it
and should delete the decorator.
"""
from __future__ import annotations

import asyncio
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

import app.fast_decide as fast_decide_module  # noqa: E402
import app.main as app_main  # noqa: E402
from app.executor import PolicyExecutor  # noqa: E402
from app.fast_decide import fast_path_eligible, is_fast_servable  # noqa: E402
from app.storage import Storage  # noqa: E402

# A trimmed copy of the seeded `bureau` connector's sample payload -- enough for
# `bureau_score` (which scans `scores` for scoreName == "CIBILTUSC3") and enough shape for
# the nested / indexed / negative-index path cases.
BUREAU_PAYLOAD = {
    "scores": [
        {"scoreName": "CIBILTUSC3", "score": "00741"},
        {"scoreName": "PLSCORE", "score": "00791"},
    ],
}
PAYLOAD = {"bureau": BUREAU_PAYLOAD, "loan": {"dti_ratio": 0.3}}

# bureau_score's value for BUREAU_PAYLOAD. Asserted against rather than recomputed so that a
# change to the seeded variable's code shows up here as a failure instead of being absorbed.
EXPECTED_BUREAU_SCORE = 741


class TransformStepTests(unittest.TestCase):

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self._orig_storage = app_main.storage
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "transform.db"))
        self.storage = app_main.storage
        self.tenant_id = self.storage.default_tenant_id
        fast_decide_module.invalidate()
        self._counter = 0

    def tearDown(self):
        fast_decide_module.invalidate()
        try:
            app_main.storage.engine.dispose()
        except Exception:  # pragma: no cover - teardown must not fail the test
            pass
        app_main.storage = self._orig_storage

    # ------------------------------------------------------------------ helpers

    def _policy(self, steps, policy_id=None):
        self._counter += 1
        policy = {
            "id": policy_id or "tx_policy_{0}".format(self._counter),
            "name": "Transform policy",
            "defaultOutcome": "review",
            "steps": steps,
        }
        self.storage.create_policy(dict(policy), tenant_id=self.tenant_id)
        return policy

    def _run(self, policy, payload=None, flag_on=False, simulate=False):
        env = dict(os.environ)
        env["COMPUTE_ONLY_USED_VARS"] = "1" if flag_on else "0"
        with mock.patch.dict(os.environ, env, clear=True):
            return asyncio.run(PolicyExecutor(self.storage).execute(
                policy=policy,
                payload=PAYLOAD if payload is None else payload,
                tenant_id=self.tenant_id,
                source="api",
                simulate=simulate,
            ))

    def _transform(self, mapping, output_key="out", extra_steps=(), **run_kwargs):
        """Run a single transform step and return the dict it wrote."""
        config = {"mapping": mapping}
        if output_key is not None:
            config["outputKey"] = output_key
        steps = [{"type": "transform", "id": "tx", "config": config}] + list(extra_steps)
        ctx = self._run(self._policy(steps), **run_kwargs)
        return ctx.transform_outputs

    # ------------------------------------------------------- a valid JSONPath mapping

    def test_a_valid_jsonpath_mapping_resolves_variables_and_payload(self):
        """The happy path, which nothing exercised before this file."""
        outputs = self._transform({
            "score": "$.variables.bureau_score",
            "dti": "$.variables.loan_dti_ratio",
            "raw": "$.payload.loan.dti_ratio",
        })
        self.assertEqual(outputs["out"]["score"], EXPECTED_BUREAU_SCORE)
        self.assertEqual(outputs["out"]["dti"], 0.3)
        self.assertEqual(outputs["out"]["raw"], 0.3)

    def test_nested_and_array_paths_including_negative_indices(self):
        """`resolve_jsonpath` supports dotted descent, `[n]`, and Python-style `[-n]`."""
        outputs = self._transform({
            "nested": "$.payload.bureau.scores[0].scoreName",
            "second": "$.payload.bureau.scores[1].score",
            "last": "$.payload.bureau.scores[-1].scoreName",
        })["out"]
        self.assertEqual(outputs["nested"], "CIBILTUSC3")
        self.assertEqual(outputs["second"], "00791")
        self.assertEqual(outputs["last"], "PLSCORE")

    def test_an_expr_mapping_is_evaluated_against_the_context(self):
        """The `{"expr": ...}` shape runs evaluate_math_expr, not resolve_jsonpath."""
        outputs = self._transform({"doubled": {"expr": "$.variables.bureau_score * 2"}})["out"]
        self.assertEqual(outputs["doubled"], EXPECTED_BUREAU_SCORE * 2)

    def test_a_literal_mapping_value_is_stored_verbatim(self):
        outputs = self._transform({"n": 42, "flag": True, "obj": {"a": 1}})["out"]
        self.assertEqual(outputs, {"n": 42, "flag": True, "obj": {"a": 1}})

    # ------------------------------------------------ mappings that resolve to nothing

    def test_a_mapping_that_resolves_to_nothing_yields_none_and_does_not_raise(self):
        """Four distinct ways to miss, all of which must be None rather than an exception.

        An out-of-range index and a descent through a scalar are the two that a naive
        implementation turns into IndexError / AttributeError, which `_run_steps` would then
        record as a step error -- and a transform is NOT in the fail-closed gate set, so the
        whole step's output would vanish rather than one field being None.
        """
        outputs = self._transform({
            "absent_variable": "$.variables.does_not_exist",
            "absent_branch": "$.payload.nosuchsource.field",
            "index_out_of_range": "$.payload.bureau.scores[9].scoreName",
            "descend_through_scalar": "$.variables.bureau_score.nope.deeper",
        })["out"]
        self.assertEqual(
            outputs,
            {"absent_variable": None, "absent_branch": None,
             "index_out_of_range": None, "descend_through_scalar": None},
        )

    def test_a_non_numeric_array_index_resolves_to_none(self):
        outputs = self._transform({"bad": "$.payload.bureau.scores[xx].scoreName"})["out"]
        self.assertIsNone(outputs["bad"])

    def test_a_path_missing_its_dollar_prefix_is_stored_as_a_literal_string(self):
        """CHARACTERISATION, not an endorsement.

        `resolve_jsonpath` is only reached for strings starting with "$." , so `"$variables.x"`
        (one missing dot) and `"variables.x"` fall through to the literal branch and are written
        into the output verbatim. Nothing validates a mapping at author time, so the first sign
        of the typo is a downstream numeric comparison against the string "$variables.x" --
        which logic.py answers False for, silently.

        Pinned here so the behaviour is at least visible; the durable fix is authoring-time
        validation of mapping values, which is out of scope for this module.
        """
        outputs = self._transform({
            "typo": "$variables.bureau_score",
            "bare": "variables.bureau_score",
        })["out"]
        self.assertEqual(outputs["typo"], "$variables.bureau_score")
        self.assertEqual(outputs["bare"], "variables.bureau_score")
        self.assertNotEqual(outputs["typo"], EXPECTED_BUREAU_SCORE)

    # ----------------------------------------------------------------- the output key

    def test_a_missing_output_key_defaults_to_transformed(self):
        """executor.py:1675 -- `config.get("outputKey", "transformed")`."""
        outputs = self._transform({"x": "$.variables.bureau_score"}, output_key=None)
        self.assertEqual(list(outputs), ["transformed"])
        self.assertEqual(outputs["transformed"]["x"], EXPECTED_BUREAU_SCORE)

    def test_two_transforms_sharing_an_output_key_overwrite_rather_than_merge(self):
        """`ctx.transform_outputs[output_key] = result` is an assignment, not an update."""
        ctx = self._run(self._policy([
            {"type": "transform", "id": "t1", "config": {"outputKey": "k", "mapping": {"a": 1}}},
            {"type": "transform", "id": "t2", "config": {"outputKey": "k", "mapping": {"b": 2}}},
        ]))
        self.assertEqual(ctx.transform_outputs["k"], {"b": 2})

    # -------------------------------------------------- what a downstream step observes

    def test_a_downstream_transform_reads_an_earlier_transforms_output(self):
        """Both the `$.transforms.<key>` and the `$.computed` views of a prior output."""
        ctx = self._run(self._policy([
            {"type": "transform", "id": "t1", "config": {
                "outputKey": "computed", "mapping": {"score": "$.variables.bureau_score"}}},
            {"type": "transform", "id": "t2", "config": {
                "outputKey": "downstream", "mapping": {
                    "via_computed": "$.computed.score",
                    "via_transforms": "$.transforms.computed.score",
                }}},
        ]))
        self.assertEqual(ctx.transform_outputs["downstream"],
                         {"via_computed": EXPECTED_BUREAU_SCORE,
                          "via_transforms": EXPECTED_BUREAU_SCORE})

    def test_a_downstream_branch_condition_sees_the_transform_output(self):
        ctx = self._run(self._policy([
            {"type": "transform", "id": "t1", "config": {
                "outputKey": "computed", "mapping": {"score": "$.variables.bureau_score"}}},
            {"type": "branch", "id": "b1", "config": {"branches": [
                {"label": "high", "condition": "computed['score'] >= 700",
                 "steps": [{"type": "outcome", "ref_id": "approve"}]},
                {"label": "low", "condition": "computed['score'] < 700",
                 "steps": [{"type": "outcome", "ref_id": "reject"}]},
            ]}},
        ]))
        self.assertEqual(ctx.outcome, "approve")

    def test_a_downstream_step_reading_a_transform_that_never_ran_sees_none(self):
        """A transform whose `expr` is not parseable raises; `_run_steps` catches it and,
        because "transform" is not in the fail-closed gate set
        (executor.py:846 -- rule / scorecard / decision_table / workflow), the run CONTINUES
        with the step's output entirely absent.

        This is defensible for a pure-compute step, but it is the precondition for the
        `computed` aliasing defect pinned below: "absent" and "empty" are not the same thing
        to `_context_view`.
        """
        ctx = self._run(self._policy([
            {"type": "transform", "id": "boom", "config": {
                "outputKey": "computed", "mapping": {"x": {"expr": "import os"}}}},
            {"type": "transform", "id": "after", "config": {
                "outputKey": "after", "mapping": {"seen": "$.computed.x"}}},
        ]))
        self.assertEqual(ctx.transform_outputs.get("computed"), None,
                         "the failed transform should have written nothing")
        self.assertIsNone(ctx.transform_outputs["after"]["seen"])
        self.assertEqual(ctx.status, "completed")
        trace = [entry for entry in ctx.step_trace if entry["step"].get("id") == "boom"]
        self.assertTrue(trace and trace[0]["error"],
                        "the failure must at least be recorded in the trace")

    # ------------------------------------------------------ fast path / executor parity

    def test_the_fast_path_declines_any_policy_holding_a_transform_step(self):
        """The fast path cannot execute a transform, so it must DECLINE rather than skip it.

        Checked under both flag states: `COMPUTE_ONLY_USED_VARS` must not be able to change
        which path serves a policy.
        """
        policy = self._policy([
            {"type": "transform", "id": "tx", "config": {
                "outputKey": "computed", "mapping": {"score": "$.variables.bureau_score"}}},
            {"type": "outcome", "ref_id": "approve"},
        ])
        for flag_on in (False, True):
            env = dict(os.environ)
            env["COMPUTE_ONLY_USED_VARS"] = "1" if flag_on else "0"
            with mock.patch.dict(os.environ, env, clear=True):
                with self.subTest(flag=flag_on):
                    self.assertFalse(is_fast_servable(policy))
                    self.assertFalse(fast_path_eligible(self.storage, policy, self.tenant_id))

    # ------------------------------------------- REGRESSIONS: defects this audit found,
    #                                              now FIXED. Each body already asserted the
    #                                              correct behaviour; they were marked
    #                                              expectedFailure only while the bug stood.
    #                                              Pinning a bug as correct is how it becomes
    #                                              permanent, so they were never written as
    #                                              assertions of the broken behaviour.

    def test_regression_transform_mapping_variables_are_not_reachable(self):
        """A transform's mapping is invisible to the reachability walk.

        `variable_reachability.collect_step_variable_ids` has an arm for rule, scorecard,
        decision_table, connector, loop, branch and workflow, plus a generic string scan over
        `config.over` / `config.condition` / `step.condition`. It has NO arm for `transform`
        and does not scan `config.mapping` -- so `$.variables.bureau_score` in a mapping never
        marks `bureau_score` as needed.

        Consequence, proven by this test: with COMPUTE_ONLY_USED_VARS=1 the variable is never
        computed, `ctx.variables` has no key for it, and the mapping silently resolves to None.
        The variable was SKIPPED rather than failed, so `ctx.variable_errors` is empty and
        `_fail_closed_on_variable_errors` never fires -- the decision is made on None with no
        signal anywhere. That is the FAST_DECIDE/flag divergence class this repo has fixed four
        times, in a fifth place.

        FIXED in app/variable_reachability.py, by extending the generic string scan to
        `config.mapping` and `config.inputMapping` values (rather than adding a
        `transform` arm) -- `model`'s inputMapping has the identical shape and the
        identical gap, so one scan closes both:

            elif step_type == "transform":
                for source_expr in (config.get("mapping") or {}).values():
                    text = source_expr.get("expr") if isinstance(source_expr, dict) else source_expr
                    if isinstance(text, str):
                        for vid in by_id:
                            if vid in text:
                                out.add(vid)

        which is the same deliberately over-inclusive substring match the module already uses
        for `config.over` and branch conditions.
        """
        policy = self._policy([
            {"type": "transform", "id": "tx", "config": {
                "outputKey": "computed", "mapping": {"score": "$.variables.bureau_score"}}},
            {"type": "outcome", "ref_id": "approve"},
        ])
        off = self._run(policy, flag_on=False).transform_outputs["computed"]["score"]
        ctx_on = self._run(policy, flag_on=True)
        on = ctx_on.transform_outputs["computed"]["score"]
        self.assertEqual(
            on, off,
            "COMPUTE_ONLY_USED_VARS changed what the transform computed: {0!r} with the flag "
            "on vs {1!r} with it off".format(on, off))
        self.assertEqual(on, EXPECTED_BUREAU_SCORE)

    def test_regression_the_reachable_set_for_a_transform_policy_is_empty(self):
        """The root cause of the two flag/simulate defects, asserted at the fix site.

        This is the unit-level statement of the same gap: `needed_variable_ids` returns the
        EMPTY SET for a policy whose only variable reader is a transform. Not a subset -- empty.
        Every variable the transform names is dropped.

        Verified against the patch quoted in
        test_regression_transform_mapping_variables_are_not_reachable: with it applied, this policy's
        reachable set becomes {bureau_score, loan_dti_ratio}, including the `{"expr": ...}` form.
        """
        from app import variable_reachability

        by_id = {v["id"]: v for v in self.storage.list_variables(tenant_id=self.tenant_id)}
        rules = {r["id"]: r for r in self.storage.list_rules(tenant_id=self.tenant_id)}
        policy = {"id": "reach", "steps": [
            {"type": "transform", "id": "tx", "config": {"outputKey": "computed", "mapping": {
                "score": "$.variables.bureau_score",
                "calc": {"expr": "$.variables.loan_dti_ratio * 2"},
            }}},
            {"type": "outcome", "ref_id": "approve"},
        ]}
        needed = variable_reachability.needed_variable_ids(policy, rules, {}, {}, by_id)
        self.assertIn("bureau_score", needed,
                      "the transform's mapping variable is unreachable, so under the flag it is "
                      "never computed and the mapping resolves to None")
        self.assertIn("loan_dti_ratio", needed,
                      "the transform's `expr` mapping variable is unreachable")

    def test_regression_simulation_diverges_from_production_for_a_transform_policy(self):
        """The same gap, but with NO flag set -- so it is not opt-in.

        executor.py:1320 filters variables when `simulate or COMPUTE_ONLY_USED_VARS=1`. Every
        backtest therefore takes the filtered path unconditionally, so a backtest of a policy
        whose transform reads `$.variables.X` computes X as None while the live decision on the
        same payload computes the real value. The backtest silently does not predict production.

        Same one-line fix as the test above.
        """
        policy = self._policy([
            {"type": "transform", "id": "tx", "config": {
                "outputKey": "computed", "mapping": {"score": "$.variables.bureau_score"}}},
            {"type": "outcome", "ref_id": "approve"},
        ])
        live = self._run(policy, flag_on=False, simulate=False)
        backtest = self._run(policy, flag_on=False, simulate=True)
        self.assertEqual(backtest.transform_outputs["computed"]["score"],
                         live.transform_outputs["computed"]["score"],
                         "a backtest and a live decision disagree about the transform's input")

    def test_regression_an_empty_computed_output_is_aliased_to_other_transform_outputs(self):
        """`_context_view` conflates "wrote an empty dict" with "wrote nothing".

        executor.py:376-377 builds two views and then exposes
        `"computed": computed or combined` (executor.py:386), where `computed` is
        `transform_outputs["computed"]` and `combined` is every transform output merged
        together. An empty dict is falsy, so a transform that explicitly writes
        `outputKey: "computed"` with an empty result does not clear `$.computed` -- it exposes
        a FLAT MERGE OF EVERY OTHER TRANSFORM'S FIELDS under that name.

        Failure scenario: a guarded transform writes `computed` only when a condition holds. On
        the branch where it writes nothing, a downstream rule reading `$.computed.limit` does
        not see None -- it sees some unrelated step's `limit`, and approves on it.

        Correct behaviour: when a step has explicitly written the `computed` key, that value
        wins, empty or not. The fallback should be keyed on the key's ABSENCE
        (`transform_outputs.get("computed") if "computed" in transform_outputs else combined`),
        not on its truthiness.
        """
        ctx = self._run(self._policy([
            {"type": "transform", "id": "t1", "config": {
                "outputKey": "other", "mapping": {"limit": 99}}},
            {"type": "transform", "id": "t2", "config": {
                "outputKey": "computed", "mapping": {}}},
            {"type": "transform", "id": "t3", "config": {
                "outputKey": "probe", "mapping": {"seen": "$.computed.limit"}}},
        ]))
        self.assertEqual(ctx.transform_outputs["computed"], {})
        self.assertIsNone(
            ctx.transform_outputs["probe"]["seen"],
            "an explicitly-empty `computed` leaked another transform's field")

    def test_regression_an_unterminated_bracket_returns_the_whole_list(self):
        """`resolve_jsonpath` (app/jsonpath.py:14) never closes an open `[`.

        The `]` branch is what consumes the index and advances `current`; with no `]` in the
        expression the loop simply ends, `token` is empty, and the function returns whatever
        `current` was when the `[` was seen -- the LIST ITSELF.

        So the typo `"$.payload.bureau.scores[0"` does not miss. It silently binds the entire
        `scores` array into the output field, where a downstream comparison or a `len()` then
        operates on a value the author never asked for. A malformed path must resolve to None,
        the same as every other malformed form (`[xx]`, out-of-range, descent through a scalar),
        all of which already do.

        Fix: track whether `index_mode` is still true when the loop ends and return None if so.
        """
        outputs = self._transform({"unterminated": "$.payload.bureau.scores[0"})["out"]
        self.assertIsNone(outputs["unterminated"])


if __name__ == "__main__":
    unittest.main()
