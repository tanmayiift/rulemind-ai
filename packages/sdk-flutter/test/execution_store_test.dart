import "dart:io";

import "package:hive/hive.dart";
import "package:rulemind/src/execution_store.dart";
import "package:rulemind/src/models.dart";
import "package:flutter_test/flutter_test.dart";

Decision _decision(
  String id, {
  String status = "completed",
  List<Map<String, dynamic>> pendingOperations = const <Map<String, dynamic>>[],
}) =>
    Decision(
      outcome: "approve",
      variables: const <String, dynamic>{},
      ruleResults: const <Map<String, dynamic>>[],
      executionId: id,
      status: status,
      pendingOperations: pendingOperations,
      policyId: "policy-1",
    );

void main() {
  late Directory tempDir;
  late ExecutionStore store;

  setUp(() async {
    tempDir = await Directory.systemTemp.createTemp("rulemind-exec-store");
    Hive.init(tempDir.path);
    store = ExecutionStore();
    await store.initialize();
  });

  tearDown(() async {
    await Hive.deleteFromDisk();
    await tempDir.delete(recursive: true);
  });

  test("persist prunes a terminal (completed, all-delivered) decision", () async {
    await store.persist(_decision("done-1"));
    expect(await store.get("done-1"), isNull);
    expect(await store.list(), isEmpty);
  });

  test("persist keeps a paused execution so it can be resumed", () async {
    await store.persist(_decision("paused-1", status: "paused"));
    final loaded = await store.get("paused-1");
    expect(loaded, isNotNull);
    expect(loaded!.status, "paused");
  });

  test("persist keeps an execution with an undelivered pending operation", () async {
    await store.persist(
      _decision(
        "pending-1",
        pendingOperations: const <Map<String, dynamic>>[
          <String, dynamic>{"id": "op-1", "status": "failed"},
        ],
      ),
    );
    expect(await store.get("pending-1"), isNotNull);
  });

  test("persist prunes an execution once every pending operation is delivered", () async {
    // First stored while a pending op is undelivered...
    await store.persist(
      _decision(
        "pending-2",
        pendingOperations: const <Map<String, dynamic>>[
          <String, dynamic>{"id": "op-1", "status": "failed"},
        ],
      ),
    );
    expect(await store.get("pending-2"), isNotNull);
    // ...then removed after its operation is delivered (lifecycle advanced to terminal).
    await store.persist(
      _decision(
        "pending-2",
        pendingOperations: const <Map<String, dynamic>>[
          <String, dynamic>{"id": "op-1", "status": "delivered"},
        ],
      ),
    );
    expect(await store.get("pending-2"), isNull);
  });

  test("the store stays bounded across many completed decisions", () async {
    for (var i = 0; i < 500; i++) {
      await store.persist(_decision("done-$i"));
    }
    expect(await store.list(), isEmpty);
  });
}
