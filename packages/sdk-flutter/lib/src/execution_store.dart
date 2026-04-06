import "package:hive/hive.dart";

import "decision_codec.dart";
import "models.dart";

class ExecutionStore {
  Box<dynamic>? _box;

  Future<void> initialize() async {
    try {
      _box ??= await Hive.openBox<dynamic>("rulemind.executions");
    } catch (_) {
      _box = null;
    }
  }

  Future<void> save(Decision decision) async {
    await initialize();
    final executionId = decision.executionId;
    if (executionId == null || executionId.isEmpty || _box == null) {
      return;
    }
    await _box!.put(executionId, decisionToMap(decision));
  }

  Future<Decision?> get(String executionId) async {
    await initialize();
    if (_box == null) return null;
    final raw = _box!.get(executionId);
    if (raw is! Map) {
      return null;
    }
    return decisionFromMap(Map<String, dynamic>.from(raw));
  }

  Future<List<Decision>> list() async {
    await initialize();
    if (_box == null) return const <Decision>[];
    return _box!.values
        .whereType<Map>()
        .map((item) => decisionFromMap(Map<String, dynamic>.from(item)))
        .toList();
  }

  Future<void> delete(String executionId) async {
    await initialize();
    if (_box == null) return;
    await _box!.delete(executionId);
  }
}
