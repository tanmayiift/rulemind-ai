import "package:workmanager/workmanager.dart";

import "bundle_manager.dart";
import "event_logger.dart";

@pragma('vm:entry-point')
void ruleMindWorkmanagerDispatcher() {
  Workmanager().executeTask((task, inputData) async {
    return true;
  });
}

class SyncService {
  SyncService({
    required this.bundleManager,
    required this.eventLogger,
  });

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
    );
  }
}
