package com.rulemind.core

import com.rulemind.core.models.ScoreFactor
import com.rulemind.core.models.ScoreRange
import com.rulemind.core.models.Scorecard
import com.rulemind.core.models.ScorecardMetric
import com.rulemind.core.models.WoeRange
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertNull
import kotlin.test.assertTrue

/**
 * Every expected number in this file was produced by running the case through the Python core
 * (`app/logic.py:evaluate_scorecard`) — the declared source of truth — not by reading the code.
 *
 * Before this, the Kotlin engine implemented POINTS ONLY: `scoring_method`, `weight`,
 * `coefficient`, `woe_values`, `intercept`, `pdo`, `target_score`, `target_odds`, `formula` and
 * `metrics` were all silently ignored, so a WoE scorecard scored differently offline than on the
 * server with no error anywhere.
 */
class ScorecardScoringMethodsTest {
    private val evaluator = ScorecardEvaluator()

    private fun score(scorecard: Scorecard, variables: Map<String, Any?>): Int =
        evaluator.evaluate(scorecard, variables)["score"] as Int

    // ── Fixtures ──

    private val points = Scorecard(
        id = "sc_points", name = "Points", baseScore = 500, maxScore = 900,
        bins = listOf(
            ScoreFactor(
                variableId = "credit_score",
                ranges = listOf(
                    ScoreRange(300.0, 549.0, -100),
                    ScoreRange(550.0, 649.0, 0),
                    ScoreRange(650.0, 749.0, 50),
                    ScoreRange(750.0, 850.0, 100),
                ),
            ),
            ScoreFactor(
                variableId = "avg_balance",
                ranges = listOf(ScoreRange(0.0, 9999.0, -50), ScoreRange(10000.0, 49999.0, 25)),
            ),
        ),
    )

    private val weighted = Scorecard(
        id = "sc_weighted", name = "Weighted", baseScore = 300, maxScore = 900,
        bins = listOf(
            ScoreFactor("credit_score", listOf(ScoreRange(700.0, 850.0, 51)), weight = 2.5),
            ScoreFactor("dti", listOf(ScoreRange(0.0, 40.0, 7)), weight = 0.5),
            ScoreFactor("delinquencies", listOf(ScoreRange(0.0, 0.0, -7)), weight = 1.5),
        ),
    )

    private val woe = Scorecard(
        id = "sc_woe", name = "WoE", baseScore = 300, maxScore = 900,
        scoringMethod = "woe", intercept = -1.2, pdo = 20.0, targetScore = 600.0, targetOdds = 50.0,
        bins = listOf(
            ScoreFactor(
                variableId = "bureau_score", ranges = emptyList(), coefficient = 1.5,
                woeValues = listOf(
                    WoeRange(0.0, 500.0, woe = -0.8, iv = 0.10, eventRate = 0.3, nonEventRate = 0.1),
                    WoeRange(500.0, 700.0, woe = 0.2, iv = 0.05, eventRate = 0.4, nonEventRate = 0.5),
                    WoeRange(700.0, 900.0, woe = 0.9, iv = 0.15, eventRate = 0.3, nonEventRate = 0.4),
                ),
            ),
            ScoreFactor(
                variableId = "age", ranges = emptyList(), coefficient = 0.8,
                woeValues = listOf(
                    WoeRange(18.0, 30.0, woe = -0.5, iv = 0.08),
                    WoeRange(30.0, 60.0, woe = 0.6, iv = 0.12),
                ),
            ),
        ),
    )

    private val formula = Scorecard(
        id = "sc_formula", name = "Formula", baseScore = 400, maxScore = 900,
        scoringMethod = "formula", formula = "base_score + total_points * 2",
        bins = listOf(ScoreFactor("credit_score", listOf(ScoreRange(700.0, 850.0, 60)))),
    )

    private val metrics = points.copy(
        id = "sc_metrics",
        metrics = listOf(
            ScorecardMetric("discount_pct", "0.15 if score > 550 else 0.05", unit = "%", category = "pricing"),
            ScorecardMetric("loan_amount", "score * 1000"),
            ScorecardMetric("ratio", "score / 4"),
            ScorecardMetric("capped", "MIN(score, 500)"),
            ScorecardMetric("broken", "score ?? 1"),
            ScorecardMetric("unknown_fn", "NOPE(score)"),
            ScorecardMetric("iv_echo", "total_iv"),
        ),
    )

    // ── Points ──

    @Test
    fun pointsMatchesPython() {
        assertEquals(625, score(points, mapOf("credit_score" to 750, "avg_balance" to 20000)))
        // Clamped at 0 is not reached here, but base 500 − 100 − 50 is.
        assertEquals(350, score(points, mapOf("credit_score" to 300, "avg_balance" to 0)))
        // A MISSING variable reads as 0 in the Python core, and 0 falls in avg_balance's first bin
        // (−50) — it is NOT "no bin". Treating it as unmatched would score 600 instead of 550.
        assertEquals(550, score(points, mapOf("credit_score" to 750)))
        // A variable explicitly set to null matches NO bin: float(None) raises and the candidate
        // is skipped, so credit_score contributes 0 rather than −100.
        assertEquals(525, score(points, mapOf("credit_score" to null, "avg_balance" to 20000)))
        assertEquals(500, score(points, mapOf("credit_score" to 999, "avg_balance" to 1_000_000_000.0)))
        assertEquals(625, score(points, mapOf("credit_score" to "750", "avg_balance" to "20000")))
        // Both bounds are INCLUSIVE and the first matching bin wins.
        assertEquals(575, score(points, mapOf("credit_score" to 650, "avg_balance" to 10000)))
        assertEquals(450, score(points, mapOf("credit_score" to 649, "avg_balance" to 9999)))
    }

    @Test
    fun pointsClampsToTheScorecardRange() {
        val tiny = points.copy(maxScore = 520)
        assertEquals(520, score(tiny, mapOf("credit_score" to 750, "avg_balance" to 20000)))
        val negative = points.copy(baseScore = 10)
        assertEquals(0, score(negative, mapOf("credit_score" to 300, "avg_balance" to 0)))
    }

    // ── Weighted points ──

    @Test
    fun weightTruncatesTowardZeroNotFloor() {
        // 300 + int(51×2.5)=127 + int(7×0.5)=3 + int(−7×1.5)=−10. Flooring the negative product
        // would give −11 and a score of 419.
        assertEquals(420, score(weighted, mapOf("credit_score" to 800, "dti" to 20, "delinquencies" to 0)))
        val result = evaluator.evaluate(weighted, mapOf("credit_score" to 800, "dti" to 20, "delinquencies" to 0))
        @Suppress("UNCHECKED_CAST")
        val breakdown = result["breakdown"] as List<Map<String, Any?>>
        assertEquals(127L, breakdown[0]["weighted_points"])
        assertEquals(-10L, breakdown[2]["weighted_points"])
    }

    // ── WoE ──

    @Test
    fun woeMatchesPython() {
        assertEquals(475, score(woe, mapOf("bureau_score" to 650, "age" to 35)))
        // Both bounds inclusive + first match wins: 500 takes the [0,500] bin (woe −0.8), and 30
        // takes [18,30] (woe −0.5). Half-open bins would land on the NEXT bin and score 507.
        assertEquals(406, score(woe, mapOf("bureau_score" to 500, "age" to 30)))
        assertEquals(452, score(woe, mapOf("bureau_score" to 1000, "age" to 5)))
        assertEquals(461, score(woe, mapOf("bureau_score" to 650)))
    }

    @Test
    fun woeRoundsHalfToEvenLikePython() {
        // pdo = 0 pins factor to 1 and target_odds = 0 pins offset to target_score, so the raw
        // score is exactly target_score + 0.5 — a tie. Python's round() breaks ties to EVEN:
        // 600.5 → 600 (down) and 601.5 → 602 (up). Half-away-from-zero would give 601 and 602.
        fun tie(target: Double) = Scorecard(
            id = "sc_tie", name = "Tie", baseScore = 0, maxScore = 900, bins = emptyList(),
            scoringMethod = "woe", pdo = 0.0, targetOdds = 0.0, targetScore = target, intercept = 0.5,
        )
        assertEquals(600, score(tie(600.0), emptyMap()))
        assertEquals(602, score(tie(601.0), emptyMap()))
    }

    @Test
    fun woeHonoursDegenerateCalibrationAndClamps() {
        assertEquals(600, score(woe.copy(pdo = 0.0, targetOdds = 0.0), mapOf("bureau_score" to 650, "age" to 35)))
        assertEquals(450, score(woe.copy(maxScore = 450), mapOf("bureau_score" to 650, "age" to 35)))
        assertEquals(0, score(woe.copy(targetScore = -10_000.0), mapOf("bureau_score" to 650, "age" to 35)))
    }

    @Test
    fun woeReportsDetailsAndInformationValue() {
        val matched = evaluator.evaluate(woe, mapOf("bureau_score" to 500, "age" to 30))
        assertEquals(0.18, matched["information_value"] as Double, 1e-9)
        @Suppress("UNCHECKED_CAST")
        val details = matched["woe_details"] as List<Map<String, Any?>>
        assertEquals(2, details.size)
        assertEquals(-0.8, details[0]["woe"])
        assertEquals(1.5 * -0.8, details[0]["contribution"])
        // No bin matched anywhere: the Python core omits both keys entirely.
        val unmatched = evaluator.evaluate(woe, mapOf("bureau_score" to 1000, "age" to 5))
        assertNull(unmatched["information_value"])
        assertNull(unmatched["woe_details"])
    }

    // ── Formula ──

    @Test
    fun formulaMatchesPython() {
        assertEquals(520, score(formula, mapOf("credit_score" to 800)))
        assertEquals(460, score(formula.copy(formula = null), mapOf("credit_score" to 800)))
        assertEquals(8, score(formula.copy(formula = "total_points / 7"), mapOf("credit_score" to 800)))
        assertEquals(775, score(formula.copy(formula = "ROUND(SQRT(total_points) * 100, 0)"), mapOf("credit_score" to 800)))
        val ternary = formula.copy(formula = "900 if credit_score >= 800 else base_score + total_points")
        assertEquals(900, score(ternary, mapOf("credit_score" to 800)))
        assertEquals(460, score(ternary, mapOf("credit_score" to 750)))
    }

    @Test
    fun aBrokenFormulaScoresBaseScoreNeverPoints() {
        // `?:` is JavaScript, not Python — the same string the TypeScript engine happily evaluates.
        // Python refuses it and the score falls back to base_score (400), NOT to the points total.
        assertEquals(400, score(formula.copy(formula = "score > 70 ? 0.15 : 0.05"), mapOf("credit_score" to 800)))
        assertEquals(400, score(formula.copy(formula = "NOPE(1)"), mapOf("credit_score" to 800)))
    }

    // ── Metrics ──

    @Test
    fun metricsMatchPython() {
        val high = evaluator.evaluate(metrics, mapOf("credit_score" to 750, "avg_balance" to 20000))
        assertEquals(625, high["score"])
        @Suppress("UNCHECKED_CAST")
        val values = high["metrics"] as Map<String, Map<String, Any?>>
        assertEquals(0.15, values["discount_pct"]!!["value"])
        assertEquals("%", values["discount_pct"]!!["unit"])
        assertEquals("pricing", values["discount_pct"]!!["category"])
        // Integer arithmetic stays integral, as in Python: 625 * 1000 is 625000, not 625000.0.
        assertEquals(625_000L, values["loan_amount"]!!["value"])
        assertEquals("financial", values["loan_amount"]!!["category"])
        assertEquals(156.25, values["ratio"]!!["value"])
        assertEquals(500.0, values["capped"]!!["value"])
        assertEquals(0.0, values["iv_echo"]!!["value"])
        // A bad metric formula records an error object — it never yields a plausible number.
        assertTrue((values["broken"]!!["value"] as Map<*, *>).containsKey("error"))
        assertTrue((values["unknown_fn"]!!["value"] as Map<*, *>).containsKey("error"))

        val low = evaluator.evaluate(metrics, mapOf("credit_score" to 300, "avg_balance" to 0))
        assertEquals(350, low["score"])
        @Suppress("UNCHECKED_CAST")
        val lowValues = low["metrics"] as Map<String, Map<String, Any?>>
        assertEquals(0.05, lowValues["discount_pct"]!!["value"])
        assertEquals(350_000L, lowValues["loan_amount"]!!["value"])
        assertEquals(87.5, lowValues["ratio"]!!["value"])
        assertEquals(350.0, lowValues["capped"]!!["value"])
    }

    // ── The formula grammar is an allowlist, not a sandbox around a general evaluator ──

    @Test
    fun theFormulaGrammarRefusesEverythingItDoesNotOptInTo() {
        // These are author-supplied strings evaluated on someone's phone. Each one must be REFUSED
        // — not best-effort evaluated — which is what the Python evaluator does with the
        // corresponding AST node. The grammar has no production for any of them, so none can even
        // be spelled: there is no attribute, subscript, call-of-a-call or assignment form.
        val hostile = listOf(
            "score.__class__", // attribute access — the first hop of the CPython subclasses walk
            "().__class__.__mro__[1].__subclasses__()",
            "variables['score']", // subscripting
            "(lambda: 1)()",
            "[x for x in variables]",
            "(y := 1)",
            "score = 1",
            "__import__('os')", // an unknown NAME, and unreachable as a call anyway
            "MIN(1,2)(3)", // calling a call result
            "score if True", // malformed conditional
        )
        for (formula in hostile) {
            val error = assertFailsWith<FormulaError>(message = "must refuse: $formula") {
                SafeFormula.evaluate(formula, mapOf("score" to 1L, "variables" to mapOf("score" to 1L)))
            }
            assertTrue(error.message!!.isNotBlank(), "refusal must say why: $formula")
        }
    }

    @Test
    fun theFormulaGrammarMatchesPythonArithmetic() {
        fun eval(formula: String) = SafeFormula.evaluate(formula, mapOf("n" to 7L, "x" to 2.5))
        assertEquals(1.5, eval("3 / 2")) // true division, always a float
        assertEquals(1L, eval("3 // 2"))
        assertEquals(-2L, eval("-3 // 2")) // FLOOR, not truncation
        assertEquals(-1L, eval("5 % -3")) // sign follows the DIVISOR
        assertEquals(512L, eval("2 ** 3 ** 2")) // right-associative
        assertEquals(-4L, eval("-2 ** 2")) // ** binds tighter than unary minus
        assertEquals(0.5, eval("2 ** -1"))
        assertEquals(true, eval("1 < n < 10")) // chained comparison
        assertEquals(false, eval("1 < n < 5"))
        assertEquals(3L, eval("1 + 2")) // int + int stays int
        assertEquals(3.5, eval("1 + x"))
        assertEquals(7L, eval("n if n > 0 else 0"))
    }

    @Test
    fun theResultCarriesTheScoringMethod() {
        assertEquals("woe", evaluator.evaluate(woe, mapOf("bureau_score" to 650))["scoring_method"])
        assertEquals("formula", evaluator.evaluate(formula, mapOf("credit_score" to 800))["scoring_method"])
        assertEquals("points", evaluator.evaluate(points, emptyMap())["scoring_method"])
    }
}
