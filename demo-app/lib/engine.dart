import 'dart:convert';
import 'dart:io' show Platform;

import 'package:http/http.dart' as http;
import 'package:rulemind/rulemind.dart';

/// Where the decision was produced. The demo can run the SAME payload through
/// both so on-device/server parity is visible rather than assumed.
enum EvalMode { server, onDevice }

/// A gate (rule step) and whether it passed, with the conditions behind it.
class GateResult {
  GateResult({required this.label, required this.passed, required this.conditions});
  final String label;
  final bool passed;
  final List<ConditionResult> conditions;
}

class ConditionResult {
  ConditionResult({
    required this.variable,
    required this.operator,
    required this.threshold,
    required this.value,
    required this.passed,
  });
  final String variable;
  final String operator;
  final Object? threshold;
  final Object? value;
  final bool passed;
}

class EvalResult {
  EvalResult({
    required this.outcome,
    required this.mode,
    this.score,
    this.latencyMs,
    this.gates = const <GateResult>[],
    this.variables = const <String, dynamic>{},
    this.error,
    this.serverOnlyStepsSkipped = const <String>[],
    this.pendingSyncCount,
  });

  final String outcome;
  final EvalMode mode;
  final num? score;
  final num? latencyMs;
  final List<GateResult> gates;
  final Map<String, dynamic> variables;
  final String? error;

  /// Workflow-only steps the on-device engine could not run.
  ///
  /// Edge mode evaluates variables, rules, scorecards and policies; workflow
  /// steps (connectors, actions, review gates) are server-only. When this is
  /// non-empty the verdict is PARTIAL, and comparing it against a server
  /// decision is not a like-for-like parity check.
  final List<String> serverOnlyStepsSkipped;

  /// Decisions queued locally and not yet accepted by the backend.
  final int? pendingSyncCount;

  bool get isError => error != null;
  List<GateResult> get failedGates => gates.where((g) => !g.passed).toList();
}

/// Default host for the local API. The Android emulator reaches the host
/// machine on 10.0.2.2; the iOS simulator shares the host loopback.
String get defaultBaseUrl {
  if (Platform.isAndroid) return 'http://10.0.2.2:8080';
  return 'http://127.0.0.1:8080';
}

const String kDefaultApiKey = 'rm_live_devlocaltenantkey000000000000';

String? lastOnDeviceStack;

class RuleMindClient {
  RuleMindClient({String? baseUrl, String? apiKey})
      : baseUrl = baseUrl ?? defaultBaseUrl,
        apiKey = apiKey ?? kDefaultApiKey;

  final String baseUrl;
  final String apiKey;
  bool _sdkReady = false;

  Future<void> ensureSdk() async {
    if (_sdkReady) return;
    // The SDK verifies every bundle it downloads and refuses one it cannot check,
    // so it needs the server's bundle-signing public key. Fetch it at init — with
    // no RULEMIND_SIGNING_PRIVATE_KEY set the server regenerates the keypair on
    // each boot, so a hardcoded copy would go stale.
    String? serverKey;
    try {
      final r = await http
          .get(Uri.parse('$baseUrl/sdk/v1/signing-key'), headers: {'x-api-key': apiKey})
          .timeout(const Duration(seconds: 10));
      if (r.statusCode == 200) {
        serverKey = (jsonDecode(r.body) as Map<String, dynamic>)['publicKeyPem'] as String?;
      }
    } catch (_) {
      // Leave null — the SDK then rejects signed bundles, which is the safe default.
    }
    await RuleMind.initialize(RuleMindConfig(
      baseUrl: baseUrl,
      apiKey: apiKey,
      serverPublicKeyPem: serverKey,
      // Fallback OFF so "on-device" genuinely means on-device — otherwise a
      // silent server round trip would masquerade as an edge decision and the
      // parity check below would be meaningless.
      enableServerFallback: false,
      // No decision cache: a demo (and a parity test) must re-evaluate every time.
      // With the default 5-minute TTL, re-running the same scenario replays the
      // previous verdict and any engine change appears to have had no effect.
      decisionCacheTtlMs: 0,
    ));
    _sdkReady = true;
  }

  // ── server path ────────────────────────────────────────────────────────
  Future<EvalResult> decideOnServer(
    String policyId,
    Map<String, dynamic> payload, {
    String? userId,
  }) async {
    final sw = Stopwatch()..start();
    try {
      final resp = await http
          .post(
            Uri.parse('$baseUrl/api/v1/decide'),
            headers: {'x-api-key': apiKey, 'content-type': 'application/json'},
            body: jsonEncode({
              'policyId': policyId,
              'payload': payload,
              if (userId != null) 'userId': userId,
            }),
          )
          .timeout(const Duration(seconds: 20));
      sw.stop();
      if (resp.statusCode != 200) {
        return EvalResult(
          outcome: 'error',
          mode: EvalMode.server,
          error: 'HTTP ${resp.statusCode}: ${_short(resp.body)}',
        );
      }
      final body = jsonDecode(resp.body) as Map<String, dynamic>;
      return EvalResult(
        outcome: (body['outcome'] ?? 'unknown').toString(),
        mode: EvalMode.server,
        score: body['score'] as num?,
        latencyMs: (body['latency_ms'] as num?) ?? sw.elapsedMilliseconds,
        gates: _gatesFromServer(body['rule_results']),
        variables: (body['variables'] as Map?)?.cast<String, dynamic>() ?? const {},
      );
    } catch (e) {
      return EvalResult(outcome: 'error', mode: EvalMode.server, error: _short('$e'));
    }
  }

  // ── on-device path (edge bundle via the Flutter SDK) ───────────────────
  Future<EvalResult> decideOnDevice(
    String policyId,
    Map<String, dynamic> payload, {
    String? userId,
  }) async {
    final sw = Stopwatch()..start();
    try {
      await ensureSdk();
      final Decision d = await RuleMind.evaluate(policyId, payload, userId: userId);
      sw.stop();

      // evaluate() enqueues completed decisions to the durable outbox, but the
      // outbox is only drained by syncPendingDecisions() or the periodic
      // background sync. Draining it here means the demo actually exercises the
      // upload path (/sdk/v1/decisions) rather than relying on a timer, and the
      // remaining depth is a truthful signal when the backend is unreachable.
      try {
        await RuleMind.syncPendingDecisions();
      } catch (_) {
        // Offline is an expected state; the outbox keeps the decision.
      }
      final int pending = await RuleMind.pendingDecisionCount();

      return EvalResult(
        outcome: d.outcome,
        mode: EvalMode.onDevice,
        score: d.score,
        latencyMs: d.latencyMs == 0 ? sw.elapsedMilliseconds : d.latencyMs,
        gates: _gatesFromSdk(d.ruleResults),
        variables: d.variables,
        serverOnlyStepsSkipped: d.serverOnlyStepsSkipped,
        pendingSyncCount: pending,
      );
    } catch (e, st) {
      lastOnDeviceStack = st.toString();
      return EvalResult(outcome: 'error', mode: EvalMode.onDevice, error: _short('$e'));
    }
  }

  Future<EvalResult> decide(
    EvalMode mode,
    String policyId,
    Map<String, dynamic> payload, {
    String? userId,
  }) =>
      mode == EvalMode.server
          ? decideOnServer(policyId, payload, userId: userId)
          : decideOnDevice(policyId, payload, userId: userId);

  Future<bool> ping() async {
    try {
      final r = await http
          .get(Uri.parse('$baseUrl/health'))
          .timeout(const Duration(seconds: 6));
      return r.statusCode == 200;
    } catch (_) {
      return false;
    }
  }

  // ── shaping ────────────────────────────────────────────────────────────
  static List<GateResult> _gatesFromServer(Object? ruleResults) {
    if (ruleResults is! List) return const [];
    return ruleResults.map((raw) {
      final m = (raw as Map).cast<String, dynamic>();
      final step = (m['step'] as Map?)?.cast<String, dynamic>() ?? const {};
      final res = (m['result'] as Map?)?.cast<String, dynamic>() ?? const {};
      return GateResult(
        label: (step['label'] ?? step['ref_id'] ?? 'Gate').toString(),
        passed: res['passed'] == true,
        conditions: _conditions(res['conditions']),
      );
    }).toList();
  }

  static List<GateResult> _gatesFromSdk(Object? ruleResults) {
    if (ruleResults is! List) return const [];
    return ruleResults.map((raw) {
      final m = (raw as Map).cast<String, dynamic>();
      return GateResult(
        label: (m['ruleName'] ?? m['ruleId'] ?? m['rule_id'] ?? 'Gate').toString(),
        passed: m['passed'] == true,
        conditions: _conditions(m['conditions']),
      );
    }).toList();
  }

  static List<ConditionResult> _conditions(Object? raw) {
    if (raw is! List) return const [];
    return raw.map((c) {
      final m = (c as Map).cast<String, dynamic>();
      return ConditionResult(
        variable: (m['variable_name'] ?? m['variableName'] ?? m['variable_id'] ?? m['variableId'] ?? '?').toString(),
        operator: (m['operator'] ?? '?').toString(),
        threshold: m['threshold'] ?? m['value'],
        value: m['value'],
        passed: m['passed'] == true,
      );
    }).toList();
  }

  static String _short(String s) => s.length <= 220 ? s : '${s.substring(0, 220)}…';
}
