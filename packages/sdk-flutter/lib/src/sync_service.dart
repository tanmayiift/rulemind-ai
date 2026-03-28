import "package:workmanager/workmanager.dart";

import "bundle_manager.dart";
import "event_logger.dart";

/// Global reference for the workmanager to call back into.
/// Set during [SyncService] construction.
BundleManager? _globalBundleManager;
EventLogger? _globalEventLogger;

@pragma('vm:entry-point')
void ruleMindWorkmanagerDispatcher() {
  Workmanager().executeTask((task, inputData) async {
    try {
      await _globalBundleManager?.syncNow();
      await _globalEventLogger?.flush();
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
  }) {
    // Store references for the workmanager dispatcher callback
    _globalBundleManager = bundleManager;
    _globalEventLogger = eventLogger;
  }

  final BundleManager bundleManager;
  final EventLogger eventLogger;

  Future<void> syncNow() async {
    await bundleManager.syncNow();
    await eventLogger.flush();
  }

  static Future<void> registerPeriodicSync({int intervalMinutes = 15}) async {
    await Workmanager().initialize(ruleMindWorkmanagerDispatcher);
    await Workmanager().registerPeriodicTask(
      "rulemind.bundle.sync",
      "rulemind.bundle.sync",
      frequency: Duration(minutes: intervalMinutes),
      constraints: Constraints(
        networkType: NetworkType.connected,
      ),
    );
  }
}
