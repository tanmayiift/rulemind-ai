import "dart:convert";

import "package:hive/hive.dart";

import "models.dart";

class DecisionCache {
  DecisionCache({
    required this.boxName,
    required this.ttlMs,
    required this.maxEntries,
  });

  final String boxName;
  final int ttlMs;
  final int maxEntries;
  Box<dynamic>? _box;
  final Map<String, Map<String, dynamic>> _memoryFallback = <String, Map<String, dynamic>>{};

  Future<void> initialize() async {
    try {
      _box ??= await Hive.openBox<dynamic>(boxName);
    } catch (_) {
      _box = null;
    }
  }

  Future<Decision?> get(String key) async {
    await initialize();
    final raw = _box?.get(key) ?? _memoryFallback[key];
    if (raw is! Map) {
      return null;
    }
    final expiresAt = raw["expiresAt"] as int? ?? 0;
    if (DateTime.now().millisecondsSinceEpoch > expiresAt) {
      await delete(key);
      return null;
    }
    return Decision(
      outcome: raw["outcome"] as String,
      score: (raw["score"] as num?)?.toDouble(),
      variables: Map<String, dynamic>.from(raw["variables"] as Map? ?? const <String, dynamic>{}),
      ruleResults: ((raw["ruleResults"] as List<dynamic>? ?? const <dynamic>[]).whereType<Map>().map((item) => Map<String, dynamic>.from(item)).toList()),
      experimentId: raw["experimentId"] as String?,
      experimentVariant: raw["experimentVariant"] as String?,
      latencyMs: raw["latencyMs"] as int? ?? 0,
      requestId: raw["requestId"] as String?,
      serverOnlyStepsSkipped: (raw["serverOnlyStepsSkipped"] as List<dynamic>? ?? const <dynamic>[]).map((item) => item.toString()).toList(),
    );
  }

  Future<void> put(String key, Decision decision) async {
    await initialize();
    final payload = <String, dynamic>{
      "outcome": decision.outcome,
      "score": decision.score,
      "variables": decision.variables,
      "ruleResults": decision.ruleResults,
      "experimentId": decision.experimentId,
      "experimentVariant": decision.experimentVariant,
      "latencyMs": decision.latencyMs,
      "requestId": decision.requestId,
      "serverOnlyStepsSkipped": decision.serverOnlyStepsSkipped,
      "expiresAt": DateTime.now().millisecondsSinceEpoch + ttlMs,
    };
    if (_box != null) {
      await _box!.put(key, payload);
      final keys = _box!.keys.cast<dynamic>().toList();
      while (keys.length > maxEntries) {
        await _box!.delete(keys.first);
        keys.removeAt(0);
      }
    } else {
      _memoryFallback[key] = payload;
      while (_memoryFallback.length > maxEntries) {
        _memoryFallback.remove(_memoryFallback.keys.first);
      }
    }
  }

  Future<void> clearCache() async {
    await initialize();
    if (_box != null) {
      await _box!.clear();
    }
    _memoryFallback.clear();
  }

  Future<void> delete(String key) async {
    await initialize();
    if (_box != null) {
      await _box!.delete(key);
    }
    _memoryFallback.remove(key);
  }

  String buildKey(String policyId, Map<String, dynamic> payload, String? experimentVariant) {
    return jsonEncode(<String, dynamic>{
      "policyId": policyId,
      "payload": payload,
      "experimentVariant": experimentVariant,
    });
  }
}
