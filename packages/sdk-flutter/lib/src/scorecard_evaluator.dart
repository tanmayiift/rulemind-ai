import "models.dart";

class ScorecardEvaluator {
  Map<String, dynamic> evaluate(Scorecard scorecard, Map<String, dynamic> variables) {
    var total = scorecard.baseScore;
    final breakdown = <Map<String, dynamic>>[];
    for (final factor in scorecard.bins) {
      final value = _numeric(variables[factor.variableId]);
      ScoreRange? activeRange;
      for (final range in factor.ranges) {
        final min = range.min ?? double.negativeInfinity;
        final max = range.max ?? double.infinity;
        if (value >= min && value <= max) {
          activeRange = range;
          break;
        }
      }
      final points = activeRange?.points ?? 0;
      total += points;
      breakdown.add(<String, dynamic>{
        "variable_id": factor.variableId,
        "value": value,
        "points": points,
        "active_range": activeRange == null
            ? null
            : <String, dynamic>{
                "min": activeRange.min,
                "max": activeRange.max,
                "points": activeRange.points,
              },
      });
    }
    return <String, dynamic>{
      "score": total.clamp(0, scorecard.maxScore),
      "base_score": scorecard.baseScore,
      "max_score": scorecard.maxScore,
      "breakdown": breakdown,
    };
  }

  double _numeric(dynamic value) {
    if (value is num) {
      return value.toDouble();
    }
    if (value is String) {
      return double.tryParse(value) ?? 0.0;
    }
    return 0.0;
  }
}
