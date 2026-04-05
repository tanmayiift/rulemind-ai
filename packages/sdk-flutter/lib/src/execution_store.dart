import "package:hive/hive.dart";

import "decision_codec.dart";
import "models.dart";

class ExecutionStore {
  Box<dynamic>? _box;

  Future<void> initialize() async {
    _box ??= await Hive.openBox<dynamic>("rulemind.executions");
  }

  Future<void> save(Decision decision) async {
    await initialize();
    final executionId = decision.executionId;
    if (executionId == null || executionId.isEmpty) {
      return;
    }
    await _box!.put(executionId, decisionToMap(decision));
  }

  Future<Decision?> get(String executionId) async {
    await initialize();
    final raw = _box!.get(executionId);
    if (raw is! Map) {
      return null;
    }
    return decisionFromMap(Map<String, dynamic>.from(raw));
  }

  Future<List<Decision>> list() async {
    await initialize();
    return _box!.values
        .whereType<Map>()
        .map((item) => decisionFromMap(Map<String, dynamic>.from(item)))
        .toList();
  }

  Future<void> delete(String executionId) async {
    await initialize();
    await _box!.delete(executionId);
  }
}
