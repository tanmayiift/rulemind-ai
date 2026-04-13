import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:rulemind/rulemind.dart';
import 'package:rulemind/src/rulemind_engine.dart';
import 'package:shared_preferences/shared_preferences.dart';

import '../studio_fixtures.dart';

class LogEntry {
  const LogEntry(this.timestamp, this.message, {this.isError = false});

  final String timestamp;
  final String message;
  final bool isError;
}

class AppState extends ChangeNotifier {
  bool isLoading = false;
  String route = 'access';
  Map<String, dynamic> content = StudioFixtures.demoContent();
  Bundle? bundle;
  String baseUrl = 'http://10.0.2.2:8080';
  String email = '';
  String password = '';
  String? mode;
  String apiKey = '';
  String? accessToken;
  String? tenantId;
  String? tenantName;
  String? userEmail;
  String? connectionStatus;
  int latestBundleVersion = 0;
  String? lastSync;
  String? currentJourneyId;
  String? error;
  List<Map<String, dynamic>> availableTenants = <Map<String, dynamic>>[];
  final Map<String, Map<String, dynamic>> drafts = <String, Map<String, dynamic>>{};
  final Map<String, Decision> decisions = <String, Decision>{};
  final List<Map<String, dynamic>> history = <Map<String, dynamic>>[];
  final List<LogEntry> logs = <LogEntry>[];
  String? selectedEntityId;
  bool isCreatingRecord = false;
  String? editingRecordId;
  final Map<String, List<Map<String, dynamic>>> adminRecords = <String, List<Map<String, dynamic>>>{};
  final Map<String, String> adminEditorRawValues = <String, String>{};
  String? adminStatus;
  Map<String, Map<String, String?>> fieldErrors = {};
  Map<String, String?> loginErrors = {};
  // Sandbox state
  String? sandboxPolicyId;
  List<Map<String, String>> sandboxFields = [{'key': '', 'value': '', 'type': 'string'}];
  Decision? sandboxResult;
  String? sandboxError;

  final RuleMindEngine _engine = RuleMindEngine();
  static const String _prefsMode = 'preferred_mode';
  static const String _prefsBaseUrl = 'base_url';
  static const String _prefsEmail = 'email';
  static const String _prefsRoute = 'route';
  static const String _prefsJourneyId = 'journey_id';
  static const String _prefsDrafts = 'drafts_json';

  String get _time {
    final now = DateTime.now();
    return '${now.hour.toString().padLeft(2, '0')}:${now.minute.toString().padLeft(2, '0')}:${now.second.toString().padLeft(2, '0')}';
  }

  List<Map<String, dynamic>> get journeys => List<Map<String, dynamic>>.from(content['journeys'] as List<dynamic>? ?? const <dynamic>[]);
  List<Map<String, dynamic>> get scenarios => List<Map<String, dynamic>>.from(content['scenarios'] as List<dynamic>? ?? const <dynamic>[]);
  List<Map<String, dynamic>> get sections => List<Map<String, dynamic>>.from(content['sections'] as List<dynamic>? ?? const <dynamic>[]);
  List<Map<String, dynamic>> get adminTools => List<Map<String, dynamic>>.from(content['adminTools'] as List<dynamic>? ?? const <dynamic>[]);
  List<Map<String, dynamic>> get adminEntities => List<Map<String, dynamic>>.from(content['adminEntities'] as List<dynamic>? ?? const <dynamic>[]);
  List<Map<String, dynamic>> get logMetrics => List<Map<String, dynamic>>.from(content['logMetrics'] as List<dynamic>? ?? const <dynamic>[]);

  void log(String message, {bool isError = false}) {
    logs.add(LogEntry(_time, message, isError: isError));
    if (logs.length > 100) {
      logs.removeRange(0, logs.length - 100);
    }
    notifyListeners();
  }

  Future<void> loadSavedConfig() async {
    final prefs = await SharedPreferences.getInstance();
    mode = prefs.getString(_prefsMode);
    baseUrl = prefs.getString(_prefsBaseUrl) ?? baseUrl;
    email = prefs.getString(_prefsEmail) ?? email;
    route = mode == 'demo' ? (prefs.getString(_prefsRoute) ?? route) : 'access';
    currentJourneyId = mode == 'demo' ? prefs.getString(_prefsJourneyId) : null;
    final rawDrafts = prefs.getString(_prefsDrafts);
    if (rawDrafts != null && rawDrafts.isNotEmpty) {
      final decoded = jsonDecode(rawDrafts) as Map<String, dynamic>;
      for (final entry in decoded.entries) {
        drafts[entry.key] = Map<String, dynamic>.from(entry.value as Map<String, dynamic>);
      }
    }
    if (mode == 'demo') {
      content = StudioFixtures.demoContent();
      bundle = StudioFixtures.demoBundle();
      tenantId = 'demo-tenant';
      tenantName = 'Local Demo Studio';
      connectionStatus = 'Offline bundle ready';
      latestBundleVersion = bundle!.bundleVersion;
    }
    notifyListeners();
  }

  Future<void> _persist() async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString(_prefsMode, mode ?? '');
    await prefs.setString(_prefsBaseUrl, baseUrl);
    await prefs.setString(_prefsEmail, email);
    await prefs.setString(_prefsRoute, route);
    await prefs.setString(_prefsJourneyId, currentJourneyId ?? '');
    await prefs.setString(_prefsDrafts, jsonEncode(drafts));
  }

  void updateBaseUrl(String value) {
    baseUrl = value;
    notifyListeners();
  }

  void updateEmail(String value) {
    email = value;
    notifyListeners();
  }

  void updatePassword(String value) {
    password = value;
    notifyListeners();
  }

  void goTo(String nextRoute, {String? journeyId}) {
    route = nextRoute;
    currentJourneyId = journeyId ?? currentJourneyId;
    notifyListeners();
  }

  Future<bool> startDemo() async {
    isLoading = true;
    error = null;
    notifyListeners();
    content = StudioFixtures.demoContent();
    bundle = StudioFixtures.demoBundle();
    mode = 'demo';
    apiKey = '';
    accessToken = null;
    tenantId = 'demo-tenant';
    tenantName = 'Local Demo Studio';
    userEmail = null;
    connectionStatus = 'Offline bundle ready';
    latestBundleVersion = bundle!.bundleVersion;
    route = 'experience_selection';
    lastSync = _time;
    password = '';
    isLoading = false;
    await _persist();
    log('Demo studio initialized with ${journeys.length} journeys and ${scenarios.length} scenarios.');
    notifyListeners();
    return true;
  }

  Future<bool> loginLive() async {
    if (baseUrl.trim().isEmpty || email.trim().isEmpty || password.trim().isEmpty) {
      error = 'Base URL, email, and password are required.';
      notifyListeners();
      return false;
    }
    final Map<String, String?> loginErrs = validateLogin();
    if (loginErrs.isNotEmpty) {
      loginErrors = loginErrs;
      error = loginErrs.values.whereType<String>().join('; ');
      notifyListeners();
      return false;
    }
    loginErrors = {};
    isLoading = true;
    error = null;
    notifyListeners();
    try {
      final response = await _requestJson(
        path: '/api/mobile/v1/auth/login',
        method: 'POST',
        body: <String, dynamic>{'email': email, 'password': password},
      ) as Map<String, dynamic>;
      accessToken = response['accessToken']?.toString();
      apiKey = response['apiKey']?.toString() ?? '';
      final Map<String, dynamic> manifest = Map<String, dynamic>.from(response['experienceManifest'] as Map<String, dynamic>? ?? <String, dynamic>{});
      final Map<String, dynamic> shell = Map<String, dynamic>.from(manifest['shell'] as Map<String, dynamic>? ?? <String, dynamic>{});
      final Map<String, dynamic> admin = Map<String, dynamic>.from(manifest['admin'] as Map<String, dynamic>? ?? <String, dynamic>{});
      final Map<String, dynamic> logsLanding = Map<String, dynamic>.from(manifest['logsLanding'] as Map<String, dynamic>? ?? <String, dynamic>{});
      content = <String, dynamic>{
        ...StudioFixtures.demoContent(),
        if (shell['appName'] != null) 'appName': shell['appName'],
        if (shell['tagline'] != null) 'tagline': shell['tagline'],
        if (shell['sections'] != null) 'sections': shell['sections'],
        if (manifest['journeys'] != null) 'journeys': manifest['journeys'],
        if (manifest['scenarios'] != null) 'scenarios': manifest['scenarios'],
        if (admin['tools'] != null) 'adminTools': admin['tools'],
        if (admin['entities'] != null) 'adminEntities': admin['entities'],
        if (logsLanding['metrics'] != null) 'logMetrics': logsLanding['metrics'],
        if (manifest['version'] != null) 'version': manifest['version'],
      };
      final tenant = Map<String, dynamic>.from(response['tenant'] as Map<String, dynamic>? ?? <String, dynamic>{});
      tenantId = tenant['id']?.toString();
      tenantName = tenant['name']?.toString();
      userEmail = (response['user'] as Map<String, dynamic>?)?['email']?.toString() ?? email;
      availableTenants = ((response['availableTenants'] as List<dynamic>?) ?? const <dynamic>[]).map((dynamic item) => Map<String, dynamic>.from(item as Map)).toList();
      mode = 'live';
      password = '';
      await RuleMind.initialize(RuleMindConfig(baseUrl: baseUrl, apiKey: apiKey));
      final health = await RuleMind.health();
      bundle = await RuleMind.syncNow() ?? bundle;
      latestBundleVersion = (health['latestBundleVersion'] as num?)?.toInt() ?? bundle?.bundleVersion ?? 0;
      connectionStatus = 'Connected • latest bundle v$latestBundleVersion';
      route = 'experience_selection';
      lastSync = _time;
      await _persist();
      isLoading = false;
      log('Live studio connected for tenant ${tenantName ?? 'Unknown Tenant'}.');
      notifyListeners();
      return true;
    } catch (e) {
      error = e.toString();
      isLoading = false;
      log('Live studio login failed: $e', isError: true);
      notifyListeners();
      return false;
    }
  }

  Future<void> syncBundle() async {
    if (mode != 'live') {
      log('Demo mode is already using the local experience bundle.');
      return;
    }
    isLoading = true;
    notifyListeners();
    try {
      final synced = await RuleMind.syncNow();
      if (synced != null) {
        bundle = synced;
        latestBundleVersion = synced.bundleVersion;
      }
      connectionStatus = 'Bundle synced • v$latestBundleVersion';
      lastSync = _time;
      log('Bundle synced: v$latestBundleVersion');
    } catch (e) {
      error = e.toString();
      log('Bundle sync failed: $e', isError: true);
    }
    isLoading = false;
    notifyListeners();
  }

  void updateDraftField(String journeyId, Map<String, dynamic> field, String value) {
    final next = Map<String, dynamic>.from(drafts[journeyId] ?? <String, dynamic>{});
    switch (field['kind']) {
      case 'number':
        next[field['id'] as String] = double.tryParse(value);
        break;
      case 'choice':
        next[field['id'] as String] = field['multi'] == true
            ? value.split(',').map((String item) => item.trim()).where((String item) => item.isNotEmpty).toList()
            : value;
        break;
      default:
        next[field['id'] as String] = value;
    }
    drafts[journeyId] = next;
    // Validate on change
    final String? error = validateField(field, next[field['id'] as String]);
    final Map<String, String?> journeyErrors = Map<String, String?>.from(fieldErrors[journeyId] ?? {});
    journeyErrors[field['id'] as String] = error;
    fieldErrors[journeyId] = journeyErrors;
    notifyListeners();
  }

  Future<bool> runScenario(String scenarioId) async {
    final scenario = scenarios.cast<Map<String, dynamic>>().firstWhere((Map<String, dynamic> item) => item['id'] == scenarioId);
    final journeyId = scenario['journeyId'] as String;
    drafts[journeyId] = Map<String, dynamic>.from((scenario['payload'] as Map<String, dynamic>)[_payloadKey(journeyId)] as Map<String, dynamic>);
    notifyListeners();
    return evaluateJourney(journeyId, explicitPayload: Map<String, dynamic>.from(scenario['payload'] as Map<String, dynamic>));
  }

  Future<bool> evaluateJourney(String journeyId, {Map<String, dynamic>? explicitPayload}) async {
    final journey = journeys.cast<Map<String, dynamic>>().firstWhere((Map<String, dynamic> item) => item['id'] == journeyId);
    isLoading = true;
    error = null;
    // Validate all fields before evaluation (skip if explicit payload from scenario)
    if (explicitPayload == null) {
      final Map<String, String?> errors = validateJourney(journeyId);
      if (errors.isNotEmpty) {
        fieldErrors[journeyId] = errors;
        error = 'Please fix validation errors before running evaluation.';
        notifyListeners();
        return false;
      }
    }
    notifyListeners();
    try {
      final payload = explicitPayload ?? _buildPayloadForJourney(journeyId);
      final Decision decision;
      if (mode == 'live') {
        decision = await RuleMind.evaluate(journey['policyId'] as String, payload, userId: 'studio_mobile_user');
      } else {
        final Bundle activeBundle = bundle ?? StudioFixtures.demoBundle();
        decision = _engine.evaluate(activeBundle, journey['policyId'] as String, payload, userId: 'demo_user');
      }
      _applyDecision(journeyId, decision);
      route = _journeyOutcomeRoute(journeyId);
      currentJourneyId = journeyId;
      isLoading = false;
      notifyListeners();
      await _persist();
      return true;
    } catch (e) {
      error = e.toString();
      isLoading = false;
      log('Journey evaluation failed: $e', isError: true);
      notifyListeners();
      return false;
    }
  }

  Future<void> flushPendingOperations(String journeyId) async {
    final Decision? current = decisions[journeyId];
    if (current == null) {
      return;
    }
    try {
      final Decision updated;
      if (mode == 'live') {
        final List<Decision> results = await RuleMind.flushPendingOperations();
        updated = results.firstWhere(
          (Decision item) => item.executionId == current.executionId || item.policyId == current.policyId,
          orElse: () => current,
        );
      } else {
        updated = _copyDecision(
          current,
          pendingOperations: current.pendingOperations.map((Map<String, dynamic> operation) => <String, dynamic>{
                ...operation,
                'status': 'delivered',
                'lastResult': <String, dynamic>{
                  'operationId': operation['id'],
                  'stepId': operation['stepId'],
                  'status': 'delivered',
                  'responseStatus': 202,
                  'responseBody': '{"simulated":true}',
                  'success': true,
                },
              }).toList(),
          actionResults: current.actionResults.map((Map<String, dynamic> item) => <String, dynamic>{
                ...item,
                'status': 'delivered',
                'responseStatus': 202,
                'responseBody': '{"simulated":true}',
                'success': true,
              }).toList(),
          auditSummary: <String, dynamic>{...current.auditSummary, 'pendingOperationCount': 0},
        );
      }
      _applyDecision(journeyId, updated);
      log('Flushed pending callbacks for ${_journeyTitle(journeyId)}.');
      notifyListeners();
    } catch (e) {
      error = e.toString();
      log('Pending operation flush failed: $e', isError: true);
      notifyListeners();
    }
  }

  Future<void> resumeReview(String journeyId, bool approve) async {
    final Decision? current = decisions[journeyId];
    if (current == null) {
      return;
    }
    try {
      final ReviewDecision reviewDecision = ReviewDecision(
        decision: approve ? 'approve' : 'reject',
        reviewerId: userEmail ?? 'studio_reviewer',
        response: <String, dynamic>{'decision_label': approve ? 'Approved' : 'Rejected'},
      );
      final Decision updated;
      if (mode == 'live' && current.executionId != null) {
        updated = await RuleMind.resumeExecution(current.executionId!, reviewDecision);
      } else {
        final Bundle activeBundle = bundle ?? StudioFixtures.demoBundle();
        updated = _engine.evaluate(
          activeBundle,
          current.policyId ?? _journeyPolicyId(journeyId),
          current.payload,
          userId: current.userId,
          resumeFrom: _copyDecision(
            current,
            status: 'running',
            pausedAtStep: null,
            currentStepIndex: (current.pausedAtStep ?? current.currentStepIndex) + 1,
            reviewTask: <String, dynamic>{...?current.reviewTask, 'status': approve ? 'approved' : 'rejected', 'reviewed_by': userEmail ?? 'studio_reviewer'},
            reviewResponse: <String, dynamic>{...current.reviewResponse, ...reviewDecision.response},
            outcome: approve ? 'approve' : 'reject',
          ),
        );
      }
      _applyDecision(journeyId, updated);
      route = _journeyAuditRoute(journeyId);
      notifyListeners();
    } catch (e) {
      error = e.toString();
      log('Review resume failed: $e', isError: true);
      notifyListeners();
    }
  }

  void selectAdminEntity(String entityId) {
    selectedEntityId = entityId;
    editingRecordId = null;
    isCreatingRecord = false;
    notifyListeners();
  }

  Future<void> loadAdminRecords(String entityId) async {
    final Map<String, dynamic> schema = _adminEntity(entityId);
    if (mode != 'live') {
      adminStatus = 'Live admin sign-in unlocks authoring and save/delete actions.';
      notifyListeners();
      return;
    }
    isLoading = true;
    notifyListeners();
    try {
      final dynamic response = await _requestJson(path: schema['listPath'] as String, method: 'GET', apiKeyOverride: apiKey);
      final List<Map<String, dynamic>> records;
      if (response is List) {
        records = response.map((dynamic item) => Map<String, dynamic>.from(item as Map)).toList();
      } else if (response is Map) {
        records = <Map<String, dynamic>>[Map<String, dynamic>.from(response as Map)];
      } else {
        records = <Map<String, dynamic>>[];
      }
      adminRecords[entityId] = records;
      adminStatus = 'Loaded ${records.length} ${schema['title'].toString().toLowerCase()}.';
    } catch (e) {
      error = e.toString();
      log('Admin list load failed: $e', isError: true);
    }
    isLoading = false;
    notifyListeners();
  }

  void startAdminCreate() {
    isCreatingRecord = true;
    editingRecordId = null;
    adminEditorRawValues.clear();
    notifyListeners();
  }

  void editAdminRecord(Map<String, dynamic> record) {
    final Map<String, dynamic> schema = _adminEntity(selectedEntityId);
    isCreatingRecord = false;
    editingRecordId = record['id']?.toString() ?? 'settings';
    adminEditorRawValues.clear();
    for (final dynamic item in (schema['fields'] as List<dynamic>? ?? const <dynamic>[])) {
      final Map<String, dynamic> field = Map<String, dynamic>.from(item as Map);
      adminEditorRawValues[field['id'] as String] = _formatFieldValue(record[field['id']], field);
    }
    notifyListeners();
  }

  void updateAdminField(Map<String, dynamic> field, String value) {
    adminEditorRawValues[field['id'] as String] = value;
    notifyListeners();
  }

  void cancelAdminEdit() {
    isCreatingRecord = false;
    editingRecordId = null;
    adminEditorRawValues.clear();
    notifyListeners();
  }

  Future<void> saveAdminRecord() async {
    final String entityId = selectedEntityId ?? '';
    if (entityId.isEmpty) {
      return;
    }
    final Map<String, dynamic> schema = _adminEntity(entityId);
    if (mode != 'live') {
      adminStatus = 'Demo mode is read-only for admin authoring.';
      notifyListeners();
      return;
    }
    final Map<String, dynamic> payload = <String, dynamic>{};
    for (final dynamic item in (schema['fields'] as List<dynamic>? ?? const <dynamic>[])) {
      final Map<String, dynamic> field = Map<String, dynamic>.from(item as Map);
      final String raw = adminEditorRawValues[field['id'] as String]?.trim() ?? '';
      if (raw.isEmpty && field['required'] != true && field['kind'] != 'boolean') {
        continue;
      }
      payload[field['id'] as String] = _parseFieldValue(field, raw);
    }
    final bool isSettings = entityId == 'settings';
    final bool isCreate = isCreatingRecord || editingRecordId == null;
    final String path = isSettings
        ? (schema['updatePath'] as String? ?? schema['detailPath'] as String)
        : isCreate
            ? schema['createPath'] as String
            : (schema['updatePath'] as String).replaceAll('{id}', editingRecordId!);
    final String method = isCreate && !isSettings ? 'POST' : 'PUT';
    try {
      await _requestJson(path: path, method: method, body: payload, apiKeyOverride: apiKey);
      adminStatus = '${schema['title'].toString().replaceAll(RegExp(r's$'), '')} saved successfully.';
      isCreatingRecord = false;
      editingRecordId = null;
      adminEditorRawValues.clear();
      await loadAdminRecords(entityId);
    } catch (e) {
      error = e.toString();
      log('Admin save failed: $e', isError: true);
    }
    notifyListeners();
  }

  Future<void> deleteAdminRecord(String recordId) async {
    final String entityId = selectedEntityId ?? '';
    if (entityId.isEmpty || mode != 'live') {
      return;
    }
    final Map<String, dynamic> schema = _adminEntity(entityId);
    try {
      await _requestJson(
        path: (schema['detailPath'] as String).replaceAll('{id}', recordId),
        method: 'DELETE',
        apiKeyOverride: apiKey,
      );
      adminStatus = '${schema['title'].toString().replaceAll(RegExp(r's$'), '')} deleted.';
      await loadAdminRecords(entityId);
    } catch (e) {
      error = e.toString();
      log('Admin delete failed: $e', isError: true);
    }
    notifyListeners();
  }

  Future<void> switchTenant(String nextTenantId) async {
    if (accessToken == null) {
      return;
    }
    try {
      final Map<String, dynamic> response = Map<String, dynamic>.from(await _requestJson(
        path: '/api/mobile/v1/tenants/$nextTenantId/session',
        method: 'POST',
        bearerToken: accessToken,
      ) as Map);
      apiKey = response['apiKey']?.toString() ?? apiKey;
      final Map<String, dynamic> tenant = Map<String, dynamic>.from(response['tenant'] as Map<String, dynamic>? ?? <String, dynamic>{});
      tenantId = tenant['id']?.toString();
      tenantName = tenant['name']?.toString();
      log('Switched admin context to ${tenantName ?? 'selected tenant'}.');
      await _persist();
    } catch (e) {
      error = e.toString();
      log('Tenant switch failed: $e', isError: true);
    }
    notifyListeners();
  }

  Future<void> logout() async {
    route = 'access';
    mode = null;
    apiKey = '';
    accessToken = null;
    tenantId = null;
    tenantName = null;
    userEmail = null;
    connectionStatus = null;
    latestBundleVersion = 0;
    availableTenants = <Map<String, dynamic>>[];
    content = StudioFixtures.demoContent();
    bundle = null;
    selectedEntityId = null;
    editingRecordId = null;
    adminEditorRawValues.clear();
    notifyListeners();
    await _persist();
  }

  void selectSandboxPolicy(String policyId) {
    sandboxPolicyId = policyId;
    sandboxResult = null;
    sandboxError = null;
    notifyListeners();
  }

  void addSandboxField() {
    sandboxFields.add({'key': '', 'value': '', 'type': 'string'});
    notifyListeners();
  }

  void updateSandboxField(int index, {String? key, String? value, String? type}) {
    if (index < 0 || index >= sandboxFields.length) return;
    final Map<String, String> f = Map<String, String>.from(sandboxFields[index]);
    if (key != null) f['key'] = key;
    if (value != null) f['value'] = value;
    if (type != null) f['type'] = type;
    sandboxFields[index] = f;
    notifyListeners();
  }

  void removeSandboxField(int index) {
    if (index >= 0 && index < sandboxFields.length) {
      sandboxFields.removeAt(index);
      notifyListeners();
    }
  }

  void clearSandbox() {
    sandboxPolicyId = null;
    sandboxFields = [{'key': '', 'value': '', 'type': 'string'}];
    sandboxResult = null;
    sandboxError = null;
    notifyListeners();
  }

  Future<void> evaluateSandbox() async {
    if (sandboxPolicyId == null || sandboxPolicyId!.isEmpty) {
      sandboxError = 'Select a policy first';
      notifyListeners();
      return;
    }
    final List<Map<String, String>> validFields = sandboxFields.where((Map<String, String> f) => (f['key'] ?? '').isNotEmpty).toList();
    if (validFields.isEmpty) {
      sandboxError = 'Add at least one field with a key';
      notifyListeners();
      return;
    }
    try {
      final Map<String, dynamic> payload = {};
      for (final Map<String, String> f in validFields) {
        final String key = f['key']!;
        final String val = f['value'] ?? '';
        switch (f['type']) {
          case 'number':
            payload[key] = double.tryParse(val) ?? val;
            break;
          case 'boolean':
            payload[key] = val.toLowerCase() == 'true';
            break;
          default:
            payload[key] = val;
        }
      }
      final Bundle activeBundle = bundle ?? StudioFixtures.demoBundle();
      final Decision decision = _engine.evaluate(activeBundle, sandboxPolicyId!, payload, userId: 'sandbox_user');
      sandboxResult = decision;
      sandboxError = null;
      log('Sandbox evaluation: outcome=${decision.outcome}, score=${decision.score}');
    } catch (e) {
      sandboxResult = null;
      sandboxError = e.toString();
      log('Sandbox evaluation failed: $e', isError: true);
    }
    notifyListeners();
  }

  Map<String, dynamic> _adminEntity(String? entityId) {
    return adminEntities.cast<Map<String, dynamic>>().firstWhere((Map<String, dynamic> item) => item['id'] == entityId, orElse: () => <String, dynamic>{});
  }

  String? validateField(Map<String, dynamic> field, dynamic value) {
    final String strVal = value?.toString() ?? '';
    final bool required = field['required'] == true;
    final String label = field['label']?.toString() ?? field['id']?.toString() ?? 'Field';

    if (required && (value == null || strVal.trim().isEmpty)) {
      return '$label is required';
    }
    if (strVal.trim().isEmpty) return null;

    if (field['kind'] == 'number') {
      final double? numVal = double.tryParse(strVal);
      if (numVal == null) return 'Must be a valid number';
      final double? minVal = (field['min'] is num) ? (field['min'] as num).toDouble() : null;
      final double? maxVal = (field['max'] is num) ? (field['max'] as num).toDouble() : null;
      if (minVal != null && numVal < minVal) return 'Must be at least $minVal';
      if (maxVal != null && numVal > maxVal) return 'Must be at most $maxVal';
    }

    final String? pattern = field['pattern']?.toString();
    if (pattern != null && pattern.isNotEmpty) {
      try {
        if (!RegExp(pattern).hasMatch(strVal)) {
          return field['patternMessage']?.toString() ?? 'Format invalid';
        }
      } catch (_) {}
    }
    return null;
  }

  Map<String, String?> validateJourney(String journeyId) {
    final Map<String, dynamic> journey = journeys.cast<Map<String, dynamic>>().firstWhere(
      (Map<String, dynamic> item) => item['id'] == journeyId,
      orElse: () => <String, dynamic>{},
    );
    final Map<String, dynamic> draft = drafts[journeyId] ?? {};
    final Map<String, String?> errors = {};
    for (final dynamic screen in (journey['screens'] as List<dynamic>? ?? [])) {
      for (final dynamic field in ((screen as Map<String, dynamic>)['fields'] as List<dynamic>? ?? [])) {
        final Map<String, dynamic> f = Map<String, dynamic>.from(field as Map);
        final String? error = validateField(f, draft[f['id']]);
        if (error != null) errors[f['id'] as String] = error;
      }
    }
    return errors;
  }

  Map<String, String?> validateLogin() {
    final Map<String, String?> errors = {};
    final RegExp emailRegex = RegExp(r'^[A-Za-z0-9+_.-]+@[A-Za-z0-9.-]+$');
    if (email.isNotEmpty && !emailRegex.hasMatch(email)) {
      errors['email'] = 'Invalid email format';
    }
    if (baseUrl.isNotEmpty) {
      try {
        final uri = Uri.parse(baseUrl);
        if (!uri.hasScheme) errors['url'] = 'Invalid URL format';
      } catch (_) {
        errors['url'] = 'Invalid URL format';
      }
    }
    return errors;
  }

  Map<String, dynamic> _buildPayloadForJourney(String journeyId) {
    final Map<String, dynamic> draft = Map<String, dynamic>.from(drafts[journeyId] ?? <String, dynamic>{});
    switch (journeyId) {
      case 'travel_guard':
        final String country = draft['destination_country']?.toString() ?? '';
        final bool medicalFlag = draft['medical_flag'] == 'Additional coverage required';
        draft['visa_ready'] = draft['visa_status'] == 'Visa held or not required' ? 1 : 0;
        draft['destination_risk'] = const <String, int>{'Japan': 2, 'United Arab Emirates': 2, 'Portugal': 1, 'France': 1, 'Switzerland': 1}[country] ?? 3;
        draft['support_matrix_ready'] = country.isEmpty ? 0 : 1;
        draft['medical_flag_value'] = medicalFlag ? 1 : 0;
        return <String, dynamic>{'travel': draft};
      case 'instant_personal_loan':
        final double monthlyIncome = _numeric(draft['monthly_income_inr']);
        final double existingEmi = _numeric(draft['existing_emi_inr']);
        final double fixedExpenses = _numeric(draft['fixed_expenses_inr']);
        final double variableExpenses = _numeric(draft['variable_expenses_inr']);
        final double housingCost = _numeric(draft['housing_cost_inr']);
        final double workYears = _numeric(draft['work_experience_years']);
        final double employerTenure = _numeric(draft['employer_tenure_months']);
        final double salaryVintage = _numeric(draft['salary_account_vintage_months']);
        final double liveness = _numeric(draft['liveness_score']);
        final bool panVerified = draft['pan_verified'] == 'Authenticated';
        final bool geoMatched = draft['geo_consistency'] == 'Matched';
        final bool utilityVerified = draft['utility_bill_verified'] == 'Yes, Verified';
        double bureauScore = 620;
        bureauScore += draft['employment_type'] == 'Salaried' ? 35 : 15;
        bureauScore += (workYears * 8).clamp(0, 60);
        bureauScore += employerTenure >= 24 ? 18 : employerTenure >= 12 ? 8 : -12;
        bureauScore += salaryVintage >= 24 ? 20 : salaryVintage >= 12 ? 8 : -10;
        bureauScore += utilityVerified ? 10 : -25;
        bureauScore += panVerified ? 12 : -85;
        bureauScore += geoMatched ? 6 : -42;
        bureauScore += liveness >= 95 ? 18 : liveness >= 90 ? 6 : -35;
        final double avgBalance = (monthlyIncome * 1.35 - (fixedExpenses + variableExpenses + existingEmi + housingCost * 0.5)).clamp(5000, 9999999);
        final double dtiRatio = monthlyIncome <= 0 ? 0 : ((existingEmi + housingCost + fixedExpenses * 0.3) / monthlyIncome).clamp(0.0, 1.0);
        draft['bureau_score'] = draft['bureau_score'] ?? bureauScore.round();
        draft['avg_balance_inr'] = draft['avg_balance_inr'] ?? avgBalance.round();
        draft['dti_ratio'] = draft['dti_ratio'] ?? _round3(dtiRatio);
        draft['pan_verified_flag'] = panVerified ? 1 : 0;
        draft['geo_consistency_flag'] = geoMatched ? 0 : 1;
        return <String, dynamic>{'loan': draft};
      case 'sme_underwriting':
        final double years = _numeric(draft['years_in_business']).clamp(1, 9999);
        final double employeeCount = _numeric(draft['employee_count']);
        final int uploadedScore = <String>['coi_uploaded', 'pan_uploaded', 'claims_report_uploaded', 'signed_declaration_uploaded']
            .where((String key) => draft[key] == 'Uploaded')
            .length;
        draft['gst_compliance_score'] = draft['gst_compliance_score'] ?? (55 + uploadedScore * 9 + years * 2).clamp(40, 98).round();
        draft['growth_rate_pct'] = draft['growth_rate_pct'] ?? _round3(((employeeCount / years) * 1.2).clamp(8, 85).toDouble());
        draft['ubo_docs_ready_flag'] = draft['ubo_docs_ready'] == 'Ready' ? 1 : 0;
        draft['claims_disclosed_flag'] = draft['claims_disclosed'] == 'Yes' ? 1 : 0;
        draft['sanctions_flag'] = draft['sanctions_flag'] ?? 0;
        return <String, dynamic>{'sme': draft};
      default:
        return <String, dynamic>{};
    }
  }

  void _applyDecision(String journeyId, Decision decision) {
    decisions[journeyId] = decision;
    history.insert(
      0,
      <String, dynamic>{
        'journeyId': journeyId,
        'executionId': decision.executionId,
        'outcome': decision.outcome,
        'status': decision.status,
        'score': decision.score,
        'source': decision.source,
        'timestamp': _time,
      },
    );
    if (history.length > 20) {
      history.removeRange(20, history.length);
    }
    log('${_journeyTitle(journeyId)}: ${decision.outcome.toUpperCase()} • ${decision.status} • score ${formatNumber(decision.score)} • ${decision.latencyMs}ms');
    if (decision.pendingOperations.isNotEmpty) {
      log('${_journeyTitle(journeyId)} queued ${decision.pendingOperations.where((Map<String, dynamic> item) => item['status'] != 'delivered').length} callback operation(s).');
    }
    if (decision.reviewTask != null && decision.status == 'paused') {
      log('${_journeyTitle(journeyId)} is waiting for manual review.');
    }
  }

  String _journeyPolicyId(String journeyId) => journeys.cast<Map<String, dynamic>>().firstWhere((Map<String, dynamic> item) => item['id'] == journeyId)['policyId'] as String;
  String _journeyTitle(String journeyId) => journeys.cast<Map<String, dynamic>>().firstWhere((Map<String, dynamic> item) => item['id'] == journeyId)['title'] as String;
  String _journeyOutcomeRoute(String journeyId) => _journeyScreenByType(journeyId, 'outcome');
  String _journeyAuditRoute(String journeyId) => _journeyScreenByType(journeyId, 'audit');
  String _payloadKey(String journeyId) => <String, String>{'travel_guard': 'travel', 'instant_personal_loan': 'loan', 'sme_underwriting': 'sme'}[journeyId] ?? journeyId;

  String _journeyScreenByType(String journeyId, String type) {
    final Map<String, dynamic> journey = journeys.cast<Map<String, dynamic>>().firstWhere((Map<String, dynamic> item) => item['id'] == journeyId);
    final List<dynamic> screens = journey['screens'] as List<dynamic>;
    return (screens.firstWhere((dynamic item) => (item as Map<String, dynamic>)['type'] == type) as Map<String, dynamic>)['id'] as String;
  }

  double _numeric(Object? value) => value is num ? value.toDouble() : double.tryParse(value?.toString() ?? '') ?? 0;
  double _round3(double value) => (value * 1000).round() / 1000;

  Future<dynamic> _requestJson({
    required String path,
    required String method,
    Map<String, dynamic>? body,
    String? bearerToken,
    String? apiKeyOverride,
  }) async {
    final HttpClient client = HttpClient();
    final Uri uri = Uri.parse('${baseUrl.replaceAll(RegExp(r"/+$"), "")}$path');
    final HttpClientRequest request = await client.openUrl(method, uri);
    request.headers.set(HttpHeaders.acceptHeader, 'application/json');
    final String? effectiveApiKey = apiKeyOverride;
    if (effectiveApiKey != null && effectiveApiKey.isNotEmpty) {
      request.headers.set('X-API-Key', effectiveApiKey);
      request.headers.set('X-SDK-Version', '4.1.0');
    }
    if (bearerToken != null && bearerToken.isNotEmpty) {
      request.headers.set(HttpHeaders.authorizationHeader, 'Bearer $bearerToken');
    }
    if (body != null) {
      request.headers.contentType = ContentType.json;
      request.write(jsonEncode(body));
    }
    final HttpClientResponse response = await request.close();
    final String raw = await utf8.decoder.bind(response).join();
    client.close(force: true);
    if (response.statusCode < 200 || response.statusCode >= 300) {
      throw StateError(raw.isEmpty ? 'HTTP ${response.statusCode}' : raw);
    }
    if (raw.isEmpty) {
      return null;
    }
    return jsonDecode(raw);
  }

  dynamic _parseFieldValue(Map<String, dynamic> field, String raw) {
    switch (field['kind']) {
      case 'number':
        final double? number = double.tryParse(raw);
        if (number == null) return 0;
        return raw.contains('.') ? number : number.toInt();
      case 'boolean':
        return raw.toLowerCase() == 'true';
      case 'choice':
        return field['multi'] == true ? raw.split(',').map((String item) => item.trim()).where((String item) => item.isNotEmpty).toList() : raw;
      case 'json':
        return jsonDecode(raw);
      default:
        return raw;
    }
  }

  String _formatFieldValue(dynamic value, Map<String, dynamic> field) {
    if (value == null) {
      return field['kind'] == 'boolean' ? 'false' : '';
    }
    if (value is List) {
      return value.join(', ');
    }
    if (value is Map) {
      return const JsonEncoder.withIndent('  ').convert(value);
    }
    return value.toString();
  }

  Decision _copyDecision(
    Decision decision, {
    String? outcome,
    double? score,
    Map<String, dynamic>? variables,
    List<Map<String, dynamic>>? ruleResults,
    int? latencyMs,
    List<String>? serverOnlyStepsSkipped,
    String? executionId,
    String? status,
    List<Map<String, dynamic>>? trace,
    Map<String, Map<String, dynamic>>? scorecardResults,
    List<Map<String, dynamic>>? actionResults,
    List<Map<String, dynamic>>? pendingOperations,
    Map<String, dynamic>? reviewTask,
    Map<String, dynamic>? explainability,
    Map<String, dynamic>? auditSummary,
    String? source,
    String? policyId,
    String? userId,
    Map<String, dynamic>? payload,
    Map<String, Map<String, dynamic>>? transformOutputs,
    int? currentStepIndex,
    int? pausedAtStep,
    Map<String, dynamic>? reviewResponse,
  }) {
    return Decision(
      outcome: outcome ?? decision.outcome,
      score: score ?? decision.score,
      variables: variables ?? decision.variables,
      ruleResults: ruleResults ?? decision.ruleResults,
      experimentId: decision.experimentId,
      experimentVariant: decision.experimentVariant,
      latencyMs: latencyMs ?? decision.latencyMs,
      requestId: decision.requestId,
      serverOnlyStepsSkipped: serverOnlyStepsSkipped ?? decision.serverOnlyStepsSkipped,
      executionId: executionId ?? decision.executionId,
      status: status ?? decision.status,
      trace: trace ?? decision.trace,
      scorecardResults: scorecardResults ?? decision.scorecardResults,
      actionResults: actionResults ?? decision.actionResults,
      pendingOperations: pendingOperations ?? decision.pendingOperations,
      reviewTask: reviewTask ?? decision.reviewTask,
      explainability: explainability ?? decision.explainability,
      auditSummary: auditSummary ?? decision.auditSummary,
      source: source ?? decision.source,
      policyId: policyId ?? decision.policyId,
      userId: userId ?? decision.userId,
      payload: payload ?? decision.payload,
      transformOutputs: transformOutputs ?? decision.transformOutputs,
      currentStepIndex: currentStepIndex ?? decision.currentStepIndex,
      pausedAtStep: pausedAtStep,
      reviewResponse: reviewResponse ?? decision.reviewResponse,
    );
  }
}

String formatNumber(double? value) {
  if (value == null) return '—';
  final double rounded = (value * 1000).round() / 1000;
  return rounded % 1 == 0 ? rounded.toInt().toString() : rounded.toString();
}
