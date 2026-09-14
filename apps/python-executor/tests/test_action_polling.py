"""Polling conditions on the `action` step.

The action step had two shapes before this: fire once with retry-on-FAILURE, and `mode: "async"`,
which pauses the execution until the provider posts back to the workflow callback endpoint.

Neither is polling, and the first test class below is the one that says why. A retry loop exits on
the first 2xx. A job-status endpoint answers `200 {"status": "pending"}` -- a perfectly successful
response carrying an unfinished result -- so the retry loop stops on attempt 1 and the policy
treats "pending" as final. Async does not help either, because it needs the provider to call back,
and the providers this is for (KYC vendors, document processors, batch bureau pulls) only offer
"POST to start, GET to check".

Egress note: `build_pinned_request` refuses loopback ("127.0.0.1 resolves to non-public address"),
so these cannot point at a local test server. The loop tests stub `_fire_action_request` -- the
seam directly under the poll -- and the condition tests call the pure predicate. What is NOT
stubbed is the thing being tested: the loop, the clamps, the budget and the fail-closed exit.
"""
import asyncio
import unittest
from unittest import mock

from app import egress
from app.executor import ActionFailedError, EvaluationBudgetExceeded, ExecutionContext, PolicyExecutor


def _ctx(budget_ms=None):
    ctx = ExecutionContext.__new__(ExecutionContext)
    ctx.tenant_id = "t"
    ctx.execution_id = "e"
    ctx.action_results = []
    ctx.step_trace = []
    ctx.outcome = "pending"
    ctx.payload = {}
    ctx.variables = {}
    ctx.budget_ms = budget_ms or 0
    ctx.deadline = None
    return ctx


def _executor():
    execu = PolicyExecutor.__new__(PolicyExecutor)
    execu._context_view = lambda c: {}
    return execu


class _Responder:
    """Stands in for _fire_action_request, returning a scripted sequence of responses.

    Returns a NEW dict each call and appends it to ctx.action_results, exactly as the real method
    does -- the poll annotates the object in place, so a stub that returned one shared dict would
    make the in-place behaviour untestable.
    """

    def __init__(self, ctx, responses):
        self.ctx = ctx
        self.responses = list(responses)
        self.calls = 0

    async def __call__(self, step, ctx, config):
        self.calls += 1
        body = self.responses[min(self.calls - 1, len(self.responses) - 1)]
        result = {"status": 200, "success": True, "body": body, "attempt": 1}
        self.ctx.action_results.append(result)
        return result


PENDING = '{"status": "pending"}'
COMPLETE = '{"status": "complete", "score": 740}'
UNTIL_COMPLETE = {"path": "body.status", "operator": "==", "value": "complete"}


class PollingIsNotRetryingTest(unittest.TestCase):
    """The distinction the whole feature rests on."""

    def test_a_successful_response_carrying_an_unfinished_result_does_not_end_a_poll(self):
        ctx = _ctx()
        execu = _executor()
        responder = _Responder(ctx, [PENDING, PENDING, COMPLETE])
        with mock.patch.object(PolicyExecutor, "_fire_action_request", responder):
            result = asyncio.run(execu._poll_action(
                {"id": "s1"}, ctx,
                {"url": "https://api.example.com/j", "mode": "poll",
                 "pollUntil": UNTIL_COMPLETE, "maxPolls": 5, "pollIntervalMs": 0}))
        self.assertEqual(responder.calls, 3, "the poll stopped before the job finished")
        self.assertTrue(result["pollSatisfied"])
        self.assertEqual(result["polls"], 3)

    def test_retry_on_failure_would_have_stopped_at_the_first_one(self):
        """The control. Every response here is a 200, so `retries` never fires: the single-shot
        path returns the pending body as the action's final result."""
        ctx = _ctx()
        execu = _executor()
        responder = _Responder(ctx, [PENDING, PENDING, COMPLETE])
        with mock.patch.object(PolicyExecutor, "_fire_action_request", responder):
            result = asyncio.run(execu._execute_action(
                {"id": "s1", "config": {"url": "https://api.example.com/j", "retries": 5}}, ctx))
        self.assertEqual(responder.calls, 1)
        self.assertEqual(result["body"], PENDING)
        self.assertNotIn("pollSatisfied", result)


class PollConditionTest(unittest.TestCase):
    """`_poll_condition_met` -- pure, so every fail-closed branch is reachable."""

    @staticmethod
    def met(condition, body, **extra):
        result = {"status": 200, "success": True, "body": body}
        result.update(extra)
        return PolicyExecutor._poll_condition_met(condition, result)

    def test_it_is_satisfied_when_the_body_matches(self):
        self.assertTrue(self.met(UNTIL_COMPLETE, COMPLETE))

    def test_it_is_not_satisfied_while_the_body_says_pending(self):
        self.assertFalse(self.met(UNTIL_COMPLETE, PENDING))

    def test_a_missing_condition_is_never_satisfied(self):
        """Fail closed. Treating "no condition" as trivially true would make a misconfigured poll
        stop on its first response and report success."""
        for condition in (None, {}, "status == complete", [], 0):
            with self.subTest(condition=condition):
                self.assertFalse(self.met(condition, COMPLETE))

    def test_a_condition_with_no_path_is_never_satisfied(self):
        self.assertFalse(self.met({"operator": "==", "value": "complete"}, COMPLETE))

    def test_a_body_that_is_not_json_is_never_satisfied(self):
        for body in ("<html>502 Bad Gateway</html>", "", "null", None):
            with self.subTest(body=body):
                self.assertFalse(self.met(UNTIL_COMPLETE, body))

    def test_a_path_that_is_not_in_the_body_is_never_satisfied(self):
        self.assertFalse(self.met({"path": "body.state", "operator": "==", "value": "complete"}, COMPLETE))

    def test_both_the_bare_and_the_jsonpath_forms_work(self):
        """The rule condition builder emits `body.status`; the docs show `$.body.status`. An
        author should not have to know which surface they are on."""
        self.assertTrue(self.met({"path": "$.body.status", "operator": "==", "value": "complete"}, COMPLETE))
        self.assertTrue(self.met({"path": "body.status", "operator": "==", "value": "complete"}, COMPLETE))

    def test_it_uses_logic_compare_rather_than_a_second_operator_set(self):
        """`>=`, `in` and `exists` are logic.py's, which is what the other four engines are held
        to by the parity corpus. A local `==` here would be a second set of semantics to keep in
        step with them."""
        self.assertTrue(self.met({"path": "body.score", "operator": ">=", "value": 700}, COMPLETE))
        self.assertFalse(self.met({"path": "body.score", "operator": ">=", "value": 800}, COMPLETE))
        self.assertTrue(self.met({"path": "body.status", "operator": "in", "value": ["complete", "done"]}, COMPLETE))
        self.assertTrue(self.met({"path": "body.score", "operator": "exists"}, COMPLETE))
        self.assertFalse(self.met({"path": "body.score", "operator": "exists"}, PENDING))

    def test_an_unknown_operator_does_not_decide_the_policy(self):
        self.assertFalse(self.met({"path": "body.status", "operator": "≈", "value": "complete"}, COMPLETE))

    def test_the_condition_can_read_the_transport_too(self):
        """Some providers signal completion with 303/201 rather than in the body."""
        self.assertTrue(PolicyExecutor._poll_condition_met(
            {"path": "status", "operator": "==", "value": 201},
            {"status": 201, "success": True, "body": PENDING}))


class PollLoopTest(unittest.TestCase):
    def _run(self, responses, **config):
        ctx = _ctx()
        execu = _executor()
        responder = _Responder(ctx, responses)
        cfg = {"url": "https://api.example.com/j", "mode": "poll",
               "pollUntil": UNTIL_COMPLETE, "pollIntervalMs": 0}
        cfg.update(config)
        with mock.patch.object(PolicyExecutor, "_fire_action_request", responder):
            result = asyncio.run(execu._poll_action({"id": "s1"}, ctx, cfg))
        return result, ctx, responder

    def test_it_stops_the_moment_the_condition_holds(self):
        result, _, responder = self._run([COMPLETE], maxPolls=5)
        self.assertEqual(responder.calls, 1)
        self.assertEqual(result["polls"], 1)

    def test_it_gives_up_after_max_polls(self):
        result, _, responder = self._run([PENDING], maxPolls=4)
        self.assertEqual(responder.calls, 4)
        self.assertFalse(result["pollSatisfied"])
        self.assertEqual(result["polls"], 4)

    def test_a_timeout_is_reported_as_failure_even_though_every_response_was_200(self):
        """The fail-closed core of it. Each attempt really did succeed at the transport level;
        `success` is overwritten because the STEP did not."""
        result, _, _ = self._run([PENDING], maxPolls=2)
        self.assertFalse(result["success"])
        self.assertIn("poll_condition_not_met", result["error"])

    def test_a_timeout_sends_the_decision_to_review_by_default(self):
        """Not `continue`, which is the single-shot default. Continuing hands the policy a
        "pending" payload, and a rule phrased `reject if status == "failed"` then APPROVES it."""
        _, ctx, _ = self._run([PENDING], maxPolls=2)
        self.assertEqual(ctx.outcome, "review")

    def test_an_author_can_opt_into_best_effort(self):
        _, ctx, _ = self._run([PENDING], maxPolls=2, onPollTimeout="continue")
        self.assertEqual(ctx.outcome, "pending")

    def test_an_author_can_opt_into_aborting(self):
        with self.assertRaises(ActionFailedError):
            self._run([PENDING], maxPolls=2, onPollTimeout="abort")

    def test_action_results_holds_one_entry_per_attempt_with_the_last_annotated(self):
        """Not N attempts plus a synthetic summary that reads like an N+1th call."""
        result, ctx, _ = self._run([PENDING], maxPolls=3)
        self.assertEqual(len(ctx.action_results), 3)
        self.assertIs(ctx.action_results[-1], result, "the returned result is not the recorded one")
        self.assertFalse(ctx.action_results[-1]["pollSatisfied"])
        self.assertNotIn("pollSatisfied", ctx.action_results[0])

    def test_the_satisfied_result_is_annotated_in_place_too(self):
        result, ctx, _ = self._run([PENDING, COMPLETE], maxPolls=3)
        self.assertIs(ctx.action_results[-1], result)
        self.assertTrue(ctx.action_results[-1]["pollSatisfied"])

    def test_both_outcomes_are_traceable(self):
        _, ctx, _ = self._run([COMPLETE], maxPolls=3)
        entry = [e for e in ctx.step_trace if e.get("type") == "action_poll"]
        self.assertEqual(len(entry), 1)
        self.assertEqual(entry[0]["status"], "satisfied")
        _, ctx, _ = self._run([PENDING], maxPolls=3)
        entry = [e for e in ctx.step_trace if e.get("type") == "action_poll"]
        self.assertEqual(entry[0]["status"], "timeout")
        self.assertEqual(entry[0]["polls"], 3)

    def test_poll_mode_is_reached_through_the_normal_step_dispatch(self):
        """_execute_action, not _poll_action directly -- otherwise the feature is unreachable."""
        ctx = _ctx()
        execu = _executor()
        responder = _Responder(ctx, [PENDING, COMPLETE])
        with mock.patch.object(PolicyExecutor, "_fire_action_request", responder):
            result = asyncio.run(execu._execute_action(
                {"id": "s1", "config": {"url": "https://api.example.com/j", "mode": "poll",
                                        "pollUntil": UNTIL_COMPLETE, "maxPolls": 3,
                                        "pollIntervalMs": 0}}, ctx))
        self.assertTrue(result["pollSatisfied"])
        self.assertEqual(responder.calls, 2)

    def test_async_mode_still_wins_over_poll(self):
        """`mode` has one value; this pins the branch order so adding poll did not shadow async."""
        ctx = _ctx()
        ctx.callbacks = {}
        ctx.status = "running"
        ctx.current_step_index = 0
        ctx.pending_operations = []
        execu = _executor()
        responder = _Responder(ctx, [COMPLETE])
        with mock.patch.object(PolicyExecutor, "_fire_action_request", responder):
            result = asyncio.run(execu._execute_action(
                {"id": "s1", "config": {"url": "https://api.example.com/j", "mode": "async"}}, ctx))
        self.assertTrue(result["paused"])


class PollBudgetTest(unittest.TestCase):
    """maxPolls x pollIntervalMs is time this server spends sleeping. T1.3 has to bound it."""

    def test_the_deadline_is_checked_before_every_attempt(self):
        ctx = _ctx()
        execu = _executor()
        ctx.budget_ms = 200
        ctx.deadline = __import__("time").perf_counter() - 0.01  # already past
        responder = _Responder(ctx, [PENDING])
        with mock.patch.object(PolicyExecutor, "_fire_action_request", responder):
            with self.assertRaises(EvaluationBudgetExceeded) as caught:
                asyncio.run(execu._poll_action(
                    {"id": "s1"}, ctx,
                    {"url": "https://api.example.com/j", "pollUntil": UNTIL_COMPLETE,
                     "maxPolls": 5, "pollIntervalMs": 0}))
        self.assertEqual(responder.calls, 0, "a request was fired with no budget left to finish it")
        self.assertIn("action poll", caught.exception.stage)

    def test_the_interval_sleep_is_clamped_to_the_remaining_budget(self):
        """Not just "it eventually raises" -- WHEN it raises.

        Without the clamp the loop sleeps the whole interval and only then finds the budget gone,
        so a 150ms budget with a 2s interval reports ~2s of elapsed time and holds the worker for
        that long. The clamp makes the exception land at the moment the budget actually ran out,
        which is also the number an operator reads when deciding whether a budget is too tight or
        one step is misbehaving.
        """
        import time as _time
        ctx = _ctx()
        execu = _executor()
        ctx.budget_ms = 150
        ctx.deadline = _time.perf_counter() + 0.15
        responder = _Responder(ctx, [PENDING])
        started = _time.perf_counter()
        with mock.patch.object(PolicyExecutor, "_fire_action_request", responder):
            with self.assertRaises(EvaluationBudgetExceeded) as caught:
                asyncio.run(execu._poll_action(
                    {"id": "s1"}, ctx,
                    {"url": "https://api.example.com/j", "pollUntil": UNTIL_COMPLETE,
                     "maxPolls": 5, "pollIntervalMs": 2000}))
        elapsed_ms = (_time.perf_counter() - started) * 1000
        self.assertLess(elapsed_ms, 800,
                        "the poll slept out its full 2s interval before noticing a 150ms budget "
                        "had run out ({0:.0f}ms)".format(elapsed_ms))
        self.assertLess(caught.exception.elapsed_ms, 800,
                        "the reported overshoot says the sleep ran past the deadline: "
                        "{0}ms against a {1}ms budget".format(
                            caught.exception.elapsed_ms, caught.exception.budget_ms))
        self.assertLess(responder.calls, 5)

    def test_no_budget_configured_still_polls(self):
        """ctx.deadline is None when evaluation_budget_ms is 0. That disables the budget, not
        the feature."""
        ctx = _ctx()
        execu = _executor()
        responder = _Responder(ctx, [PENDING, COMPLETE])
        with mock.patch.object(PolicyExecutor, "_fire_action_request", responder):
            result = asyncio.run(execu._poll_action(
                {"id": "s1"}, ctx,
                {"url": "https://api.example.com/j", "pollUntil": UNTIL_COMPLETE,
                 "maxPolls": 3, "pollIntervalMs": 0}))
        self.assertTrue(result["pollSatisfied"])


class PollClampTest(unittest.TestCase):
    """The same argument as clamp_timeout_ms/clamp_retries, and it bites harder here: a poll is a
    loop whose exit condition the REMOTE side controls."""

    def test_max_polls_is_capped(self):
        self.assertEqual(egress.clamp_polls(10 ** 6), egress.MAX_POLLS)

    def test_max_polls_floors_at_one_not_zero(self):
        """A poll step that never fires is not a degraded poll -- it is a step that silently did
        nothing while reporting a result."""
        for value in (0, -5):
            with self.subTest(value=value):
                self.assertEqual(egress.clamp_polls(value), 1)

    def test_garbage_falls_back_to_the_default(self):
        for value in (None, "soon", {}, [1]):
            with self.subTest(value=value):
                self.assertEqual(egress.clamp_polls(value), 3)
                self.assertEqual(egress.clamp_poll_interval_ms(value), 1000)

    def test_the_interval_is_capped(self):
        self.assertEqual(egress.clamp_poll_interval_ms(10 ** 6), egress.MAX_POLL_INTERVAL_MS)

    def test_a_zero_interval_is_allowed(self):
        """Unlike maxPolls, 0 is meaningful: poll as fast as the budget allows."""
        self.assertEqual(egress.clamp_poll_interval_ms(0), 0)

    def test_the_clamps_are_actually_applied_by_the_loop(self):
        ctx = _ctx()
        execu = _executor()
        responder = _Responder(ctx, [PENDING])
        with mock.patch.object(PolicyExecutor, "_fire_action_request", responder):
            asyncio.run(execu._poll_action(
                {"id": "s1"}, ctx,
                {"url": "https://api.example.com/j", "pollUntil": UNTIL_COMPLETE,
                 "maxPolls": 10 ** 6, "pollIntervalMs": 0}))
        self.assertEqual(responder.calls, egress.MAX_POLLS)


class PurecoreStillDefersAPollTest(unittest.TestCase):
    """The cross-engine half.

    Polling is host-side work: `core/engine.py` performs no I/O and lists "action" in
    _DEFERRED_STEP_TYPES, and the TypeScript and Rust engines do not know the step type at all.
    So `mode: "poll"` is not a new semantics for them to implement -- but it MUST NOT change what
    they already do. A new config key that pushed a deferred step into the `else` branch would
    make it undecidable, and an undecidable step fails the whole decision closed to review.
    """

    def test_a_poll_configured_action_is_deferred_exactly_as_a_plain_one_is(self):
        from app.core.engine import decide
        from tests.test_core_engine import sample_bundle

        results = {}
        for label, config in (
            ("plain", {"url": "https://api.example.com/j"}),
            ("poll", {"url": "https://api.example.com/j", "mode": "poll",
                      "pollUntil": UNTIL_COMPLETE, "maxPolls": 5}),
        ):
            bundle = sample_bundle()
            bundle["policy"]["steps"] = [
                {"type": "action", "ref_id": "notify", "config": config},
                {"type": "outcome", "ref_id": "approve"},
            ]
            out = decide(bundle, {"annual_income": 120000, "risk_flag": False}, {})
            entry = next(t for t in out["trace"] if t.get("step", {}).get("type") == "action")
            results[label] = (out["outcome"], out["input_valid"], entry.get("deferred"),
                              entry.get("undecidable"))

        self.assertEqual(results["poll"], results["plain"],
                         "adding poll config changed what the pure core does with an action step")
        self.assertTrue(results["poll"][2], "the action step stopped being deferred")
        self.assertIsNone(results["poll"][3])


if __name__ == "__main__":
    unittest.main()
