import 'dart:convert';

import 'package:http/http.dart' as http;

import 'src/bundle_manager.dart';
import 'src/decision_cache.dart';
import 'src/event_logger.dart';
import 'src/models.dart';
import 'src/rulemind_engine.dart';
import 'src/sync_service.dart';

export 'src/models.dart';

class RuleMind {
  RuleMind._();

  static RuleMindConfig? _config;
  static BundleManager? _bundleManager;
  static EventLogger? _eventLogger;
  static RuleMindEngine _engine = RuleMindEngine();
  static DecisionCache? _decisionCache;
  static http.Client _httpClient = http.Client();

  static Future<void> initialize(RuleMindConfig config) async {
    _config = config;
    _bundleManager = BundleManager(config: config, httpClient: _httpClient);
    _eventLogger = EventLogger(config: config, httpClient: _httpClient);
    SyncService(bundleManager: _bundleManager!, eventLogger: _eventLogger!);
    _decisionCache = DecisionCache(
      boxName: "rulemind.decisions",
      ttlMs: config.decisionCacheTtlMs,
      maxEntries: config.decisionCacheMaxEntries,
    );
    await _decisionCache!.initialize();
    await _eventLogger!.initialize();
    await SyncService.registerPeriodicSync(intervalMinutes: config.bundleSyncIntervalMinutes);
  }

  static Future<Bundle?> syncNow() async {
    return _bundleManager?.syncNow();
  }

  static Future<Decision> evaluate(String policyId, Map<String, dynamic> payload, {String? userId}) async {
    final config = _requireConfig();
    Bundle? bundle = await _bundleManager?.currentBundle();
    bundle ??= await syncNow();
    if (bundle == null && config.enableServerFallback) {
      return _serverDecide(policyId, payload, userId: userId);
    }
    final activeBundle = bundle!;
    Experiment? experiment;
    for (final item in activeBundle.experiments) {
      if (item.status == "running" && item.targetPolicyId == policyId) {
        experiment = item;
        break;
      }
    }
    final variant = experiment == null ? null : _engine.getExperiment(activeBundle, experiment.id, userId);
    final cacheKey = _decisionCache!.buildKey(policyId, payload, variant?.id);
    final cached = await _decisionCache!.get(cacheKey);
    if (cached != null) {
      return cached;
    }
    final decision = _engine.evaluate(activeBundle, policyId, payload, userId: userId);
    await _decisionCache!.put(cacheKey, decision);
    return decision;
  }

  static Future<ExperimentVariant?> getExperiment(String experimentId, String? userId) async {
    final bundle = await _bundleManager?.currentBundle();
    if (bundle == null) {
      return null;
    }
    return _engine.getExperiment(bundle, experimentId, userId);
  }

  static Future<void> logEvent(String type, Map<String, dynamic> data) async {
    await _eventLogger?.queue(type, data);
  }

  static Future<void> clearCache() async {
    await _decisionCache?.clearCache();
  }

  static RuleMindConfig _requireConfig() {
    final config = _config;
    if (config == null) {
      throw StateError('RuleMind.initialize() must be called first.');
    }
    return config;
  }

  static Future<Decision> _serverDecide(String policyId, Map<String, dynamic> payload, {String? userId}) async {
    final config = _requireConfig();
    final response = await _httpClient.post(
      Uri.parse('${config.baseUrl.replaceAll(RegExp(r'/+$'), '')}/sdk/v1/decide'),
      headers: <String, String>{
        'Content-Type': 'application/json',
        'X-API-Key': config.apiKey,
        'X-SDK-Version': config.sdkVersion,
      },
      body: jsonEncode(<String, dynamic>{
        'policyId': policyId,
        'payload': payload,
        'userId': userId,
        'sdkVersion': config.sdkVersion,
      }),
    );
    final json = jsonDecode(response.body) as Map<String, dynamic>;
    return Decision(
      outcome: json['outcome'] as String,
      score: (json['score'] as num?)?.toDouble(),
      variables: (json['variables'] as Map<String, dynamic>?) ?? const <String, dynamic>{},
      ruleResults: ((json['ruleResults'] as List<dynamic>? ?? const <dynamic>[]).whereType<Map<String, dynamic>>().toList()),
      experimentId: json['experimentId'] as String?,
      experimentVariant: json['experimentVariant'] as String?,
      latencyMs: (json['latencyMs'] as num?)?.toInt() ?? 0,
      requestId: json['requestId'] as String?,
      serverOnlyStepsSkipped: (json['serverOnlyStepsSkipped'] as List<dynamic>? ?? const <dynamic>[]).map((item) => item.toString()).toList(),
    );
  }
}
