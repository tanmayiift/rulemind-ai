import "package:flutter_test/flutter_test.dart";
import "package:rulemind/src/models.dart";
import "package:rulemind/src/safe_formula.dart";
import "package:rulemind/src/scorecard_evaluator.dart";

/// Every expected number in this file was produced by running the case through the Python core
/// (`app/logic.py:evaluate_scorecard`) — the declared source of truth — not by reading the code.
///
/// Before this, the Dart engine implemented POINTS ONLY: `scoring_method`, `weight`,
/// `coefficient`, `woe_values`, `intercept`, `pdo`, `target_score`, `target_odds`, `formula` and
/// `metrics` were all silently ignored, so a WoE scorecard scored differently offline than on the
/// server with no error anywhere.
void main() {
  final evaluator = ScorecardEvaluator();
  int score(Scorecard scorecard, Map<String, dynamic> variables) =>
      evaluator.evaluate(scorecard, variables)["score"] as int;

  Scorecard points({int baseScore = 500, int maxScore = 900, List<ScorecardMetric> metrics = const <ScorecardMetric>[]}) =>
      Scorecard(
        id: "sc_points",
        name: "Points",
        baseScore: baseScore,
        maxScore: maxScore,
        metrics: metrics,
        bins: <ScoreFactor>[
          ScoreFactor(
            variableId: "credit_score",
            ranges: <ScoreRange>[
              ScoreRange(min: 300, max: 549, points: -100),
              ScoreRange(min: 550, max: 649, points: 0),
              ScoreRange(min: 650, max: 749, points: 50),
              ScoreRange(min: 750, max: 850, points: 100),
            ],
          ),
          ScoreFactor(
            variableId: "avg_balance",
            ranges: <ScoreRange>[
              ScoreRange(min: 0, max: 9999, points: -50),
              ScoreRange(min: 10000, max: 49999, points: 25),
            ],
          ),
        ],
      );

  final weighted = Scorecard(
    id: "sc_weighted",
    name: "Weighted",
    bins: <ScoreFactor>[
      ScoreFactor(variableId: "credit_score", weight: 2.5, ranges: <ScoreRange>[ScoreRange(min: 700, max: 850, points: 51)]),
      ScoreFactor(variableId: "dti", weight: 0.5, ranges: <ScoreRange>[ScoreRange(min: 0, max: 40, points: 7)]),
      ScoreFactor(variableId: "delinquencies", weight: 1.5, ranges: <ScoreRange>[ScoreRange(min: 0, max: 0, points: -7)]),
    ],
  );

  Scorecard woe({
    int maxScore = 900,
    double pdo = 20.0,
    double targetOdds = 50.0,
    double targetScore = 600.0,
  }) =>
      Scorecard(
        id: "sc_woe",
        name: "WoE",
        maxScore: maxScore,
        scoringMethod: "woe",
        intercept: -1.2,
        pdo: pdo,
        targetScore: targetScore,
        targetOdds: targetOdds,
        bins: <ScoreFactor>[
          ScoreFactor(
            variableId: "bureau_score",
            coefficient: 1.5,
            ranges: const <ScoreRange>[],
            woeValues: <WoeRange>[
              WoeRange(min: 0, max: 500, woe: -0.8, iv: 0.10, eventRate: 0.3, nonEventRate: 0.1),
              WoeRange(min: 500, max: 700, woe: 0.2, iv: 0.05, eventRate: 0.4, nonEventRate: 0.5),
              WoeRange(min: 700, max: 900, woe: 0.9, iv: 0.15, eventRate: 0.3, nonEventRate: 0.4),
            ],
          ),
          ScoreFactor(
            variableId: "age",
            coefficient: 0.8,
            ranges: const <ScoreRange>[],
            woeValues: <WoeRange>[
              WoeRange(min: 18, max: 30, woe: -0.5, iv: 0.08),
              WoeRange(min: 30, max: 60, woe: 0.6, iv: 0.12),
            ],
          ),
        ],
      );

  Scorecard formula(String? expression) => Scorecard(
        id: "sc_formula",
        name: "Formula",
        baseScore: 400,
        scoringMethod: "formula",
        formula: expression,
        bins: <ScoreFactor>[
          ScoreFactor(variableId: "credit_score", ranges: <ScoreRange>[ScoreRange(min: 700, max: 850, points: 60)]),
        ],
      );

  group("points", () {
    test("matches the Python core", () {
      expect(score(points(), <String, dynamic>{"credit_score": 750, "avg_balance": 20000}), 625);
      expect(score(points(), <String, dynamic>{"credit_score": 300, "avg_balance": 0}), 350);
      // A MISSING variable reads as 0 in the Python core, and 0 falls in avg_balance's first bin
      // (-50) — it is NOT "no bin". Treating it as unmatched would score 600 instead of 550.
      expect(score(points(), <String, dynamic>{"credit_score": 750}), 550);
      // A variable explicitly set to null matches NO bin: float(None) raises and the candidate is
      // skipped, so credit_score contributes 0 rather than -100.
      expect(score(points(), <String, dynamic>{"credit_score": null, "avg_balance": 20000}), 525);
      expect(score(points(), <String, dynamic>{"credit_score": 999, "avg_balance": 1000000000.0}), 500);
      expect(score(points(), <String, dynamic>{"credit_score": "750", "avg_balance": "20000"}), 625);
      // Both bounds are INCLUSIVE and the first matching bin wins.
      expect(score(points(), <String, dynamic>{"credit_score": 650, "avg_balance": 10000}), 575);
      expect(score(points(), <String, dynamic>{"credit_score": 649, "avg_balance": 9999}), 450);
    });

    test("clamps to the scorecard range", () {
      expect(score(points(maxScore: 520), <String, dynamic>{"credit_score": 750, "avg_balance": 20000}), 520);
      expect(score(points(baseScore: 10), <String, dynamic>{"credit_score": 300, "avg_balance": 0}), 0);
    });
  });

  test("weight truncates toward zero, not floor", () {
    // 300 + int(51x2.5)=127 + int(7x0.5)=3 + int(-7x1.5)=-10. Flooring the negative product would
    // give -11 and a score of 419.
    final variables = <String, dynamic>{"credit_score": 800, "dti": 20, "delinquencies": 0};
    expect(score(weighted, variables), 420);
    final breakdown = evaluator.evaluate(weighted, variables)["breakdown"] as List<dynamic>;
    expect((breakdown[0] as Map)["weighted_points"], 127);
    expect((breakdown[2] as Map)["weighted_points"], -10);
  });

  group("woe", () {
    test("matches the Python core", () {
      expect(score(woe(), <String, dynamic>{"bureau_score": 650, "age": 35}), 475);
      // Both bounds inclusive + first match wins: 500 takes the [0,500] bin (woe -0.8) and 30
      // takes [18,30] (woe -0.5). Half-open bins would land on the NEXT bin and score 507.
      expect(score(woe(), <String, dynamic>{"bureau_score": 500, "age": 30}), 406);
      expect(score(woe(), <String, dynamic>{"bureau_score": 1000, "age": 5}), 452);
      expect(score(woe(), <String, dynamic>{"bureau_score": 650}), 461);
    });

    test("rounds half to EVEN like Python", () {
      // pdo = 0 pins factor to 1 and target_odds = 0 pins offset to target_score, so the raw score
      // is exactly target_score + 0.5 — a tie. Python's round() breaks ties to EVEN: 600.5 -> 600
      // (down) and 601.5 -> 602 (up). Half-away-from-zero would give 601 and 602.
      Scorecard tie(double target) => Scorecard(
            id: "sc_tie",
            name: "Tie",
            baseScore: 0,
            bins: const <ScoreFactor>[],
            scoringMethod: "woe",
            pdo: 0,
            targetOdds: 0,
            targetScore: target,
            intercept: 0.5,
          );
      expect(score(tie(600), const <String, dynamic>{}), 600);
      expect(score(tie(601), const <String, dynamic>{}), 602);
    });

    test("honours degenerate calibration and clamps", () {
      expect(score(woe(pdo: 0, targetOdds: 0), <String, dynamic>{"bureau_score": 650, "age": 35}), 600);
      expect(score(woe(maxScore: 450), <String, dynamic>{"bureau_score": 650, "age": 35}), 450);
      expect(score(woe(targetScore: -10000), <String, dynamic>{"bureau_score": 650, "age": 35}), 0);
    });

    test("reports details and the information value", () {
      final matched = evaluator.evaluate(woe(), <String, dynamic>{"bureau_score": 500, "age": 30});
      expect(matched["information_value"] as double, closeTo(0.18, 1e-9));
      final details = matched["woe_details"] as List<dynamic>;
      expect(details.length, 2);
      expect((details[0] as Map)["woe"], -0.8);
      expect((details[0] as Map)["contribution"], 1.5 * -0.8);
      // No bin matched anywhere: the Python core omits both keys entirely.
      final unmatched = evaluator.evaluate(woe(), <String, dynamic>{"bureau_score": 1000, "age": 5});
      expect(unmatched.containsKey("woe_details"), isFalse);
      expect(unmatched.containsKey("information_value"), isFalse);
    });
  });

  group("formula", () {
    test("matches the Python core", () {
      expect(score(formula("base_score + total_points * 2"), <String, dynamic>{"credit_score": 800}), 520);
      expect(score(formula(null), <String, dynamic>{"credit_score": 800}), 460);
      expect(score(formula("total_points / 7"), <String, dynamic>{"credit_score": 800}), 8);
      expect(score(formula("ROUND(SQRT(total_points) * 100, 0)"), <String, dynamic>{"credit_score": 800}), 775);
      final ternary = formula("900 if credit_score >= 800 else base_score + total_points");
      expect(score(ternary, <String, dynamic>{"credit_score": 800}), 900);
      expect(score(ternary, <String, dynamic>{"credit_score": 750}), 460);
    });

    test("a broken formula scores base_score, never points", () {
      // `?:` is JavaScript, not Python — the same string the TypeScript engine happily evaluates.
      // Python refuses it and the score falls back to base_score (400), NOT to the points total.
      expect(score(formula("score > 70 ? 0.15 : 0.05"), <String, dynamic>{"credit_score": 800}), 400);
      expect(score(formula("NOPE(1)"), <String, dynamic>{"credit_score": 800}), 400);
    });
  });

  test("metrics match the Python core", () {
    final card = points(metrics: <ScorecardMetric>[
      ScorecardMetric(name: "discount_pct", formula: "0.15 if score > 550 else 0.05", unit: "%", category: "pricing"),
      ScorecardMetric(name: "loan_amount", formula: "score * 1000"),
      ScorecardMetric(name: "ratio", formula: "score / 4"),
      ScorecardMetric(name: "capped", formula: "MIN(score, 500)"),
      ScorecardMetric(name: "broken", formula: "score ?? 1"),
      ScorecardMetric(name: "unknown_fn", formula: "NOPE(score)"),
      ScorecardMetric(name: "iv_echo", formula: "total_iv"),
    ]);

    final high = evaluator.evaluate(card, <String, dynamic>{"credit_score": 750, "avg_balance": 20000});
    expect(high["score"], 625);
    final values = high["metrics"] as Map<String, dynamic>;
    expect((values["discount_pct"] as Map)["value"], 0.15);
    expect((values["discount_pct"] as Map)["unit"], "%");
    expect((values["discount_pct"] as Map)["category"], "pricing");
    // Integer arithmetic stays integral, as in Python: 625 * 1000 is 625000, not 625000.0.
    expect((values["loan_amount"] as Map)["value"], 625000);
    expect((values["loan_amount"] as Map)["value"], isA<int>());
    expect((values["ratio"] as Map)["value"], 156.25);
    expect((values["capped"] as Map)["value"], 500.0);
    expect((values["iv_echo"] as Map)["value"], 0.0);
    // A bad metric formula records an error object — it never yields a plausible number.
    expect(((values["broken"] as Map)["value"] as Map).containsKey("error"), isTrue);
    expect(((values["unknown_fn"] as Map)["value"] as Map).containsKey("error"), isTrue);

    final low = evaluator.evaluate(card, <String, dynamic>{"credit_score": 300, "avg_balance": 0});
    expect(low["score"], 350);
    final lowValues = low["metrics"] as Map<String, dynamic>;
    expect((lowValues["discount_pct"] as Map)["value"], 0.05);
    expect((lowValues["loan_amount"] as Map)["value"], 350000);
    expect((lowValues["ratio"] as Map)["value"], 87.5);
    expect((lowValues["capped"] as Map)["value"], 350.0);
  });

  group("formula grammar", () {
    test("refuses everything it does not opt in to", () {
      // These are author-supplied strings evaluated on someone's phone. Each one must be REFUSED
      // — not best-effort evaluated — which is what the Python evaluator does with the
      // corresponding AST node. The grammar has no production for any of them, so none can even
      // be spelled: there is no attribute, subscript, call-of-a-call or assignment form.
      const hostile = <String>[
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
      ];
      for (final formula in hostile) {
        expect(
          () => evaluateFormula(formula, <String, Object?>{
            "score": 1,
            "variables": <String, Object?>{"score": 1},
          }),
          throwsA(isA<FormulaError>()),
          reason: "must refuse: $formula",
        );
      }
    });

    test("matches Python arithmetic", () {
      Object? eval(String formula) => evaluateFormula(formula, <String, Object?>{"n": 7, "x": 2.5});
      expect(eval("3 / 2"), 1.5); // true division, always a double
      expect(eval("3 // 2"), 1);
      expect(eval("-3 // 2"), -2); // FLOOR, not truncation
      expect(eval("5 % -3"), -1); // sign follows the DIVISOR
      expect(eval("2 ** 3 ** 2"), 512); // right-associative
      expect(eval("-2 ** 2"), -4); // ** binds tighter than unary minus
      expect(eval("2 ** -1"), 0.5);
      expect(eval("1 < n < 10"), isTrue); // chained comparison
      expect(eval("1 < n < 5"), isFalse);
      expect(eval("1 + 2"), 3); // int + int stays int
      expect(eval("1 + 2"), isA<int>());
      expect(eval("1 + x"), 3.5);
      expect(eval("n if n > 0 else 0"), 7);
    });
  });

  test("the result carries the scoring method", () {
    expect(evaluator.evaluate(woe(), <String, dynamic>{"bureau_score": 650})["scoring_method"], "woe");
    expect(evaluator.evaluate(formula(null), <String, dynamic>{"credit_score": 800})["scoring_method"], "formula");
    expect(evaluator.evaluate(points(), const <String, dynamic>{})["scoring_method"], "points");
  });
}
