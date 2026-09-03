import "dart:convert";
import "dart:io";

import "package:flutter_test/flutter_test.dart";
import "package:rulemind/src/models.dart";
import "package:rulemind/src/scorecard_evaluator.dart";

/// Dart arm of the cross-engine SCORECARD conformance suite. Loads the shared fixture
/// (packages/shared/scorecards.spec.json), whose every `expected` block was produced by CALLING
/// the Python core (`app/logic.py evaluate_scorecard`), and asserts the offline engine reproduces
/// the score exactly for every case across all four scoring methods — plus the breakdown, the WoE
/// details, the information value and the derived metrics.
///
/// Until this existed there was no shared scorecard corpus at all, which is why the offline engine
/// could implement points-only for so long without a single test noticing.
void main() {
  final spec = jsonDecode(_locateSpec().readAsStringSync()) as Map<String, dynamic>;
  final cases = (spec["cases"] as List).cast<Map<String, dynamic>>();
  final tolerance = ((spec["meta"] as Map)["floatTolerance"] as num?)?.toDouble() ?? 1e-9;
  final evaluator = ScorecardEvaluator();

  void expectNear(Object? expected, Object? actual, String reason) {
    expect(expected, isA<num>(), reason: "$reason: corpus value is not numeric");
    expect(actual, isA<num>(), reason: "$reason: expected a number, got $actual");
    expect((expected! as num).toDouble(), closeTo((actual! as num).toDouble(), tolerance), reason: reason);
  }

  /// Numbers compare within tolerance regardless of int/double shape; everything else exactly.
  void expectSame(Object? expected, Object? actual, String reason) {
    if (expected == null) {
      expect(actual, isNull, reason: reason);
      return;
    }
    if (expected is num && actual is num) {
      expect(expected.toDouble(), closeTo(actual.toDouble(), tolerance), reason: reason);
      return;
    }
    expect(actual, expected, reason: reason);
  }

  /// A bin bound: numeric after coercion, so `"0"` (the corpus echoes the RAW range dict) and
  /// `0.0` (the typed model parses bounds on the way in) agree. The bin match turns on the number.
  void expectSameBound(Object? expected, Object? actual, String reason) {
    if (expected == null) {
      expect(actual, isNull, reason: reason);
      return;
    }
    final want = expected is num ? expected.toDouble() : double.tryParse(expected.toString().trim());
    final got = actual is num ? actual.toDouble() : double.tryParse(actual.toString().trim());
    expect(want, isNotNull, reason: "$reason: corpus bound $expected is not numeric");
    expect(got, isNotNull, reason: "$reason: expected $expected but was $actual");
    expect(want!, closeTo(got!, tolerance), reason: reason);
  }

  test("every case reproduces the Python score", () {
    expect(cases, isNotEmpty, reason: "the corpus must not be empty");
    var woeCases = 0;
    var formulaCases = 0;
    var metricCases = 0;

    for (var i = 0; i < cases.length; i += 1) {
      final testCase = cases[i];
      final label = "case ${testCase["name"] ?? i}";
      final scorecard = Scorecard.fromJson(testCase["scorecard"] as Map<String, dynamic>);
      final variables = (testCase["variables"] as Map).cast<String, dynamic>();
      final lookup = ((testCase["variableLookup"] as Map?) ?? const <String, dynamic>{})
          .map((key, value) => MapEntry(key.toString(), (value as Map).cast<String, dynamic>()));
      final expected = testCase["expected"] as Map<String, dynamic>;

      final actual = evaluator.evaluate(scorecard, variables, lookup);

      expect(actual["score"], expected["score"], reason: "$label score");
      expect(actual["scoring_method"], expected["scoringMethod"], reason: "$label scoring_method");
      expect(actual["base_score"], expected["baseScore"], reason: "$label base_score");
      expect(actual["max_score"], expected["maxScore"], reason: "$label max_score");

      final breakdown = (actual["breakdown"] as List).cast<Map<String, dynamic>>();
      expect(breakdown.length, expected["breakdownLength"], reason: "$label breakdown length");

      final wantBreakdown = (expected["breakdown"] as List).cast<Map<String, dynamic>>();
      for (var b = 0; b < wantBreakdown.length; b += 1) {
        final want = wantBreakdown[b];
        final got = breakdown[b];
        final at = "$label breakdown[$b]";
        expect(got["variable_id"], want["variableId"], reason: "$at variable_id");
        expect(got["variable_name"], want["variableName"], reason: "$at variable_name");
        expect(got["source_id"], want["sourceId"], reason: "$at source_id");
        expect(got["points"], want["points"], reason: "$at points");
        expect(got["weighted_points"], want["weightedPoints"], reason: "$at weighted_points");
        expectNear(want["weight"], got["weight"], "$at weight");
        expectNear(want["coefficient"], got["coefficient"], "$at coefficient");
        expectNear(want["woe"], got["woe"], "$at woe");
        expectSame(want["value"], got["value"], "$at value");

        final wantRange = want["activeRange"] as Map<String, dynamic>?;
        final gotRange = got["active_range"] as Map<String, dynamic>?;
        if (wantRange == null) {
          expect(gotRange, isNull, reason: "$at active_range must be null");
        } else {
          expect(gotRange, isNotNull, reason: "$at active_range");
          expectSameBound(wantRange["min"], gotRange!["min"], "$at active_range.min");
          expectSameBound(wantRange["max"], gotRange["max"], "$at active_range.max");
          // `points` is likewise raw in the corpus (7.9) and typed as int in the model, matching
          // Python's own `int(active_range.get("points", 0))` on the way to a score.
          expectSameBound(_truncated(wantRange["points"]), gotRange["points"], "$at active_range.points");
        }
      }

      if (scorecard.scoringMethod == "woe") woeCases += 1;
      if (scorecard.scoringMethod == "formula") formulaCases += 1;

      if (expected["hasWoeDetails"] == true) {
        final details = actual["woe_details"] as List?;
        expect(details, isNotNull, reason: "$label woe_details");
        expectNear(expected["informationValue"], actual["information_value"], "$label information_value");
        final wantDetails = (expected["woeDetails"] as List).cast<Map<String, dynamic>>();
        expect(details!.length, wantDetails.length, reason: "$label woe_details length");
        for (var d = 0; d < wantDetails.length; d += 1) {
          final want = wantDetails[d];
          final got = details[d] as Map<String, dynamic>;
          final at = "$label woe_details[$d]";
          expect(got["variable_id"], want["variable_id"], reason: "$at variable_id");
          expectNear(want["woe"], got["woe"], "$at woe");
          expectNear(want["iv"], got["iv"], "$at iv");
          expectNear(want["coefficient"], got["coefficient"], "$at coefficient");
          expectNear(want["contribution"], got["contribution"], "$at contribution");
          expectSame(want["event_rate"], got["event_rate"], "$at event_rate");
          expectSame(want["non_event_rate"], got["non_event_rate"], "$at non_event_rate");
        }
      } else {
        // Absent entirely when nothing matched — not present-and-zero.
        expect(actual.containsKey("woe_details"), isFalse, reason: "$label woe_details must be absent");
        expect(actual.containsKey("information_value"), isFalse, reason: "$label information_value must be absent");
      }

      if (expected["hasMetrics"] == true) {
        metricCases += 1;
        final metrics = actual["metrics"] as Map<String, dynamic>?;
        expect(metrics, isNotNull, reason: "$label metrics");
        final wantMetrics = (expected["metrics"] as Map).cast<String, dynamic>();
        expect(metrics!.keys.toSet(), wantMetrics.keys.toSet(), reason: "$label metric names");
        for (final name in wantMetrics.keys) {
          final want = wantMetrics[name] as Map<String, dynamic>;
          final got = metrics[name] as Map<String, dynamic>;
          final at = "$label metrics[$name]";
          expect(got["unit"], want["unit"], reason: "$at unit");
          expect(got["category"], want["category"], reason: "$at category");
          final wantValue = want["value"];
          if (wantValue is Map && wantValue.containsKey("error")) {
            // The corpus records only THAT a metric failed — the real message embeds CPython's
            // SyntaxError text, which no other engine can reproduce. What must match is that the
            // metric produced an error object rather than a plausible number.
            expect(got["value"], isA<Map>(), reason: "$at should be an error, was ${got["value"]}");
            expect((got["value"] as Map).containsKey("error"), isTrue, reason: "$at should be an error");
          } else {
            expectSame(wantValue, got["value"], "$at value");
          }
        }
      } else {
        expect(actual.containsKey("metrics"), isFalse, reason: "$label metrics must be absent");
      }
    }

    // Guard against a corpus that quietly stops exercising the methods this test exists for.
    expect(woeCases, greaterThan(0), reason: "corpus contains no WoE cases");
    expect(formulaCases, greaterThan(0), reason: "corpus contains no formula cases");
    expect(metricCases, greaterThan(0), reason: "corpus contains no metric cases");
  });
}

Object? _truncated(Object? raw) {
  final number = raw is num ? raw.toDouble() : double.tryParse(raw?.toString().trim() ?? "");
  return number == null ? raw : number.truncate();
}

File _locateSpec() {
  for (final candidate in <String>[
    "../shared/scorecards.spec.json",
    "../../packages/shared/scorecards.spec.json",
    "packages/shared/scorecards.spec.json",
  ]) {
    final file = File(candidate);
    if (file.existsSync()) return file;
  }
  throw StateError("Could not locate packages/shared/scorecards.spec.json");
}
