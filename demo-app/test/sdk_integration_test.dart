import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:rulemind_journeys/engine.dart';
import 'package:rulemind_journeys/main.dart';

Widget _wrap(Widget child) => MaterialApp(
      home: Scaffold(body: ListView(children: <Widget>[child])),
    );

void main() {

  group('a partial on-device decision is disclosed, not hidden', () {
    testWidgets('renders the skipped-steps banner naming each step',
        (WidgetTester tester) async {
      await tester.pumpWidget(_wrap(ResultCard(
        result: EvalResult(
          outcome: 'approve',
          mode: EvalMode.onDevice,
          score: 700,
          serverOnlyStepsSkipped: <String>['kyc_connector', 'manual_review_gate'],
        ),
        expected: 'approve',
      )));
      await tester.pump();

      expect(find.textContaining('Partial decision'), findsOneWidget);
      expect(find.textContaining('2 server-only steps skipped'), findsOneWidget);
      // The steps must be named: "some steps were skipped" is not actionable.
      expect(find.textContaining('kyc_connector'), findsOneWidget);
      expect(find.textContaining('manual_review_gate'), findsOneWidget);
    });

    testWidgets('does NOT claim "as expected" when the decision is partial',
        (WidgetTester tester) async {
      await tester.pumpWidget(_wrap(ResultCard(
        result: EvalResult(
          outcome: 'approve',
          mode: EvalMode.onDevice,
          serverOnlyStepsSkipped: <String>['kyc_connector'],
        ),
        // The outcome matches, so the old code would have shown a green
        // "as expected" tick even though workflow steps never ran.
        expected: 'approve',
      )));
      await tester.pump();

      expect(find.text('as expected'), findsNothing,
          reason: 'a partial decision is not comparable to a server decision');
      expect(find.text('partial — not comparable'), findsOneWidget);
    });

    testWidgets('a complete decision still reports parity normally',
        (WidgetTester tester) async {
      await tester.pumpWidget(_wrap(ResultCard(
        result: EvalResult(outcome: 'approve', mode: EvalMode.onDevice),
        expected: 'approve',
      )));
      await tester.pump();

      expect(find.text('as expected'), findsOneWidget);
      expect(find.textContaining('Partial decision'), findsNothing);
    });
  });

  group('outbox depth is visible', () {
    testWidgets('queued decisions are shown as a backlog',
        (WidgetTester tester) async {
      await tester.pumpWidget(_wrap(ResultCard(
        result: EvalResult(
          outcome: 'approve',
          mode: EvalMode.onDevice,
          pendingSyncCount: 3,
        ),
        expected: null,
      )));
      await tester.pump();
      expect(find.text('3 queued'), findsOneWidget);
    });

    testWidgets('a drained outbox reads as synced', (WidgetTester tester) async {
      await tester.pumpWidget(_wrap(ResultCard(
        result: EvalResult(
          outcome: 'approve',
          mode: EvalMode.onDevice,
          pendingSyncCount: 0,
        ),
        expected: null,
      )));
      await tester.pump();
      expect(find.text('synced'), findsOneWidget);
    });
  });
}
