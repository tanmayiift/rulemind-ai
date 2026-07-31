import "package:workmanager/workmanager.dart";

import "bundle_manager.dart";
import "decision_syncer.dart";
import "event_logger.dart";

/// Global reference for the workmanager to call back into.
/// Set during [SyncService] construction.
BundleManager? _globalBundleManager;
EventLogger? _globalEventLogger;
DecisionSyncer? _globalDecisionSyncer;

@pragma('vm:entry-point')
void ruleMindWorkmanagerDispatcher() {
  Workmanager().executeTask((task, inputData) async {
    try {
      await _globalBundleManager?.syncNow();
      await _globalEventLogger?.flush();
      await _globalDecisionSyncer?.sync(); // drain the durable on-device decision outbox
    } catch (_) {
      // Swallow errors in background task — will retry next cycle
    }
    return true;
  });
}

class SyncService {
  SyncService({
    required this.bundleManager,
    required this.eventLogger,
    this.decisionSyncer,
  }) {
    // Store references for the workmanager dispatcher callback
    _globalBundleManager = bundleManager;
    _globalEventLogger = eventLogger;
    _globalDecisionSyncer = decisionSyncer;
  }

  final BundleManager bundleManager;
  final EventLogger eventLogger;
  final DecisionSyncer? decisionSyncer;

  Future<void> syncNow() async {
    await bundleManager.syncNow();
    await eventLogger.flush();
    await decisionSyncer?.sync();
  }

  static Future<void> registerPeriodicSync({int intervalMinutes = 15}) async {
    try {
      await Workmanager().initialize(ruleMindWorkmanagerDispatcher);
      await Workmanager().registerPeriodicTask(
        "rulemind.bundle.sync",
        "rulemind.bundle.sync",
        frequency: Duration(minutes: intervalMinutes),
        constraints: Constraints(
          networkType: NetworkType.connected,
        ),
      );
    } catch (_) {
      // WorkManager may not be available in test or desktop environments.
    }
  }
}
