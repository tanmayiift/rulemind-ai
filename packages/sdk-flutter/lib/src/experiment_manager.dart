import "dart:convert";

import "package:crypto/crypto.dart";

import "models.dart";

class ExperimentManager {
  ExperimentVariant? assign(Experiment experiment, String? userId) {
    if (experiment.status != "running" || userId == null || userId.isEmpty || experiment.variants.isEmpty) {
      return null;
    }
    final digest = md5.convert(utf8.encode("${experiment.id}:$userId")).toString();
    final bucket = BigInt.parse(digest, radix: 16) % BigInt.from(100);
    var cumulative = 0;
    for (final variant in experiment.variants) {
      cumulative += variant.weight;
      if (bucket.toInt() < cumulative) {
        return variant;
      }
    }
    return experiment.variants.last;
  }
}
