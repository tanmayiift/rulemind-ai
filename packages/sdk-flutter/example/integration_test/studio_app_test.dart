import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:hive/hive.dart';
import 'package:integration_test/integration_test.dart';
import 'package:rulemind/rulemind.dart';
import 'package:rulemind_example/main.dart';
import 'package:shared_preferences/shared_preferences.dart';

const String _backendUrl = String.fromEnvironment('RM_E2E_BASE_URL', defaultValue: 'http://10.0.2.2:8080');
const String _adminEmail = String.fromEnvironment('RM_E2E_ADMIN_EMAIL', defaultValue: 'admin@rulemind.local');
const String _adminPassword = String.fromEnvironment('RM_E2E_ADMIN_PASSWORD', defaultValue: 'rulemind-admin');

void main() {
  IntegrationTestWidgetsFlutterBinding.ensureInitialized();

  setUp(() async {
    // Reset SDK state and Hive between tests to avoid leaked frames/boxes.
    await RuleMind.resetForTest();
    final dir = Directory.systemTemp.createTempSync('rulemind_test_');
    Hive.init(dir.path);
  });

  testWidgets('demo scenario supports review resume and audit flow', (WidgetTester tester) async {
    await _resetState();
    await tester.pumpWidget(const RuleMindStudioApp());
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const ValueKey<String>('action_enter_demo')));
    await tester.pumpAndSettle();
    await _pumpUntilFound(tester, find.byKey(const ValueKey<String>('route_experience_selection')));

    await tester.tap(find.byKey(const ValueKey<String>('action_open_home_dashboard')));
    await tester.pumpAndSettle();
    await _pumpUntilFound(tester, find.byKey(const ValueKey<String>('route_home_dashboard')));

    await tester.tap(find.byKey(const ValueKey<String>('action_open_scenarios')));
    await tester.pumpAndSettle();
    await _pumpUntilFound(tester, find.byKey(const ValueKey<String>('route_scenario_hub')));

    await tester.tap(find.byKey(const ValueKey<String>('action_run_scenario_travel_senior_review')));
    await tester.pumpAndSettle(const Duration(milliseconds: 250));
    await _pumpUntilFound(tester, find.byKey(const ValueKey<String>('decision_summary')));

    expect(find.byKey(const ValueKey<String>('action_review_approve')), findsOneWidget);
    await tester.tap(find.byKey(const ValueKey<String>('action_review_approve')));
    await tester.pumpAndSettle(const Duration(milliseconds: 250));
    await _pumpUntilFound(tester, find.textContaining('Status: completed'));

    await tester.tap(find.byKey(const ValueKey<String>('action_open_audit')));
    await tester.pumpAndSettle();
    await _pumpUntilFound(tester, find.byKey(const ValueKey<String>('trace_item_0')));
  });

  testWidgets('demo scenario flushes pending callbacks', (WidgetTester tester) async {
    await _resetState();
    await tester.pumpWidget(const RuleMindStudioApp());
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const ValueKey<String>('action_enter_demo')));
    await tester.pumpAndSettle();
    await _pumpUntilFound(tester, find.byKey(const ValueKey<String>('route_experience_selection')));

    await tester.tap(find.byKey(const ValueKey<String>('action_open_home_dashboard')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const ValueKey<String>('action_open_scenarios')));
    await tester.pumpAndSettle();
    await _pumpUntilFound(tester, find.byKey(const ValueKey<String>('route_scenario_hub')));

    await tester.tap(find.byKey(const ValueKey<String>('action_run_scenario_loan_prime_approved')));
    await tester.pumpAndSettle(const Duration(milliseconds: 250));
    await _pumpUntilFound(tester, find.byKey(const ValueKey<String>('decision_summary')));
    await _pumpUntilFound(tester, find.byKey(const ValueKey<String>('action_flush_pending_callbacks')));

    await tester.tap(find.byKey(const ValueKey<String>('action_flush_pending_callbacks')));
    await tester.pumpAndSettle(const Duration(milliseconds: 250));
    await _pumpUntilAbsent(tester, find.byKey(const ValueKey<String>('action_flush_pending_callbacks')));
  });

  testWidgets('live admin smoke works when backend is reachable', (WidgetTester tester) async {
    if (!await _backendReachable(_backendUrl)) {
      return;
    }

    await _resetState();
    await tester.pumpWidget(const RuleMindStudioApp());
    await tester.pumpAndSettle();

    await tester.enterText(find.byKey(const ValueKey<String>('input_server_url')), _backendUrl);
    await tester.enterText(find.byKey(const ValueKey<String>('input_email')), _adminEmail);
    await tester.enterText(find.byKey(const ValueKey<String>('input_password')), _adminPassword);
    await tester.tap(find.byKey(const ValueKey<String>('action_connect_live')));
    await tester.pumpAndSettle(const Duration(milliseconds: 250));
    await _pumpUntilFound(tester, find.byKey(const ValueKey<String>('route_experience_selection')), timeout: const Duration(seconds: 20));

    await tester.tap(find.byKey(const ValueKey<String>('action_open_home_dashboard')));
    await tester.pumpAndSettle();
    await _pumpUntilFound(tester, find.byKey(const ValueKey<String>('route_home_dashboard')));

    await tester.tap(find.text('Admin'));
    await tester.pumpAndSettle();
    await _pumpUntilFound(tester, find.byKey(const ValueKey<String>('route_admin_console')));
    expect(find.byKey(const ValueKey<String>('admin_entity_rules')), findsOneWidget);
    expect(find.byKey(const ValueKey<String>('action_admin_refresh')), findsOneWidget);

    await tester.tap(find.text('Logs'));
    await tester.pumpAndSettle();
    await _pumpUntilFound(tester, find.byKey(const ValueKey<String>('route_logs_explainability')));
    expect(find.byKey(const ValueKey<String>('status_connection')), findsOneWidget);
  });
}

Future<void> _resetState() async {
  final SharedPreferences prefs = await SharedPreferences.getInstance();
  await prefs.clear();
}

Future<void> _pumpUntilFound(
  WidgetTester tester,
  Finder finder, {
  Duration timeout = const Duration(seconds: 30),
}) async {
  final DateTime deadline = DateTime.now().add(timeout);
  while (DateTime.now().isBefore(deadline)) {
    await tester.pump(const Duration(milliseconds: 200));
    if (finder.evaluate().isNotEmpty) {
      return;
    }
  }
  expect(finder, findsOneWidget);
}

Future<void> _pumpUntilAbsent(
  WidgetTester tester,
  Finder finder, {
  Duration timeout = const Duration(seconds: 30),
}) async {
  final DateTime deadline = DateTime.now().add(timeout);
  while (DateTime.now().isBefore(deadline)) {
    await tester.pump(const Duration(milliseconds: 200));
    if (finder.evaluate().isEmpty) {
      return;
    }
  }
  expect(finder, findsNothing);
}

Future<bool> _backendReachable(String baseUrl) async {
  final HttpClient client = HttpClient();
  try {
    final Uri uri = Uri.parse('${baseUrl.replaceAll(RegExp(r'/$'), '')}/health');
    final HttpClientRequest request = await client.getUrl(uri).timeout(const Duration(seconds: 3));
    final HttpClientResponse response = await request.close().timeout(const Duration(seconds: 3));
    return response.statusCode >= 200 && response.statusCode < 300;
  } catch (_) {
    return false;
  } finally {
    client.close(force: true);
  }
}
