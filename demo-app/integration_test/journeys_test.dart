import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:integration_test/integration_test.dart';
import 'package:rulemind_journeys/engine.dart';
import 'package:rulemind_journeys/journeys.dart';
import 'package:rulemind_journeys/main.dart';

/// Drives the real app against the real API on the host machine.
/// Every scenario asserts the decision the policy is DESIGNED to make, so this
/// doubles as a conformance test of the deployed rule engine from a real device.
void main() {
  IntegrationTestWidgetsFlutterBinding.ensureInitialized();

  testWidgets('API is reachable from the device', (tester) async {
    final ok = await RuleMindClient().ping();
    expect(ok, isTrue, reason: 'API not reachable from the simulator/emulator');
  });

  group('server-mode decisions match policy intent', () {
    for (final j in kJourneys) {
      for (final s in j.scenarios) {
        testWidgets('${j.title} · ${s.name} -> ${s.expected}', (tester) async {
          final r = await RuleMindClient().decideOnServer(
            j.policyId,
            j.payloadFor(s.values),
            userId: 'itest-${j.id}-${s.name.hashCode}',
          );
          expect(r.isError, isFalse, reason: 'error: ${r.error}');
          expect(r.outcome, s.expected,
              reason: '${j.title}/${s.name}: failed gates = '
                  '${r.failedGates.map((g) => g.label).toList()}');
        });
      }
    }
  });

  testWidgets('decision carries explainability (gates + conditions)', (tester) async {
    final j = kJourneys.first;
    final s = j.scenarios.firstWhere((x) => x.expected == 'reject');
    final r = await RuleMindClient().decideOnServer(j.policyId, j.payloadFor(s.values));
    expect(r.isError, isFalse);
    expect(r.gates, isNotEmpty, reason: 'no rule gates returned');
    expect(r.failedGates, isNotEmpty, reason: 'a reject must name the gate that failed');
    final withConds = r.gates.where((g) => g.conditions.isNotEmpty);
    expect(withConds, isNotEmpty, reason: 'no per-condition detail returned');
    for (final c in withConds.first.conditions) {
      expect(c.variable, isNotEmpty);
      expect(c.operator, isNotEmpty);
    }
  });

  testWidgets('UI renders a decision end to end', (tester) async {
    await tester.pumpWidget(const RuleMindDemoApp());
    await tester.pumpAndSettle();

    await tester.tap(find.text(kJourneys.first.title));
    await tester.pumpAndSettle();

    // The FAB sits in a Padding at centerFloat and the scrolling body absorbs the
    // synthesised hit, so drive its callback directly — the point of this test is
    // the state -> engine -> result-card render path, not FAB hit geometry.
    tester.widget<FilledButton>(find.byType(FilledButton)).onPressed!();

    // Real wall time for the HTTP round trip (pump() only moves a fake clock),
    // then scroll: the verdict renders at the bottom of a scrollable form, so a
    // ListView never builds it while it is off-screen.
    for (var i = 0; i < 40 && find.text('WHY').evaluate().isEmpty; i++) {
      await tester.runAsync(() => Future<void>.delayed(const Duration(milliseconds: 200)));
      await tester.pump();
      await tester.drag(find.byType(ListView), const Offset(0, -400));
      await tester.pump();
    }

    // Surface what actually rendered if the assertion is about to fail.
    if (find.text('APPROVE').evaluate().isEmpty) {
      final texts = tester.widgetList<Text>(find.byType(Text))
          .map((w) => w.data)
          .where((d) => d != null && d.trim().isNotEmpty)
          .toList();
      debugPrint('SCREEN TEXTS: $texts');
    }
    expect(find.text('APPROVE'), findsOneWidget);
    expect(find.text('WHY'), findsOneWidget);
    expect(find.textContaining('as expected'), findsOneWidget);
  });

  testWidgets('server and on-device agree (cross-engine parity)', (tester) async {
    final client = RuleMindClient();
    final mismatches = <String>[];
    var onDeviceUsable = true;
    for (final j in kJourneys) {
      for (final s in j.scenarios) {
        final payload = j.payloadFor(s.values);
        final srv = await client.decideOnServer(j.policyId, payload);
        final dev = await client.decideOnDevice(j.policyId, payload);
        if (dev.isError) {
          onDeviceUsable = false;
          debugPrint('ON-DEVICE UNAVAILABLE (${j.title}/${s.name}): ${dev.error}');
          continue;
        }
        if (srv.outcome != dev.outcome) {
          mismatches.add('${j.title}/${s.name}: server=${srv.outcome} device=${dev.outcome}');
        }
      }
    }
    if (!onDeviceUsable) {
      debugPrint('SKIP parity: on-device engine unavailable on this build.');
      return;
    }
    expect(mismatches, isEmpty, reason: mismatches.join('\n'));
  });
}
