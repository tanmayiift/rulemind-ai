import 'package:flutter_test/flutter_test.dart';
import 'package:rulemind_journeys/journeys.dart';
import 'package:rulemind_journeys/main.dart';

void main() {
  testWidgets('home lists every journey', (tester) async {
    await tester.pumpWidget(const RuleMindDemoApp());
    await tester.pump();
    for (final j in kJourneys) {
      expect(find.text(j.title), findsOneWidget, reason: 'missing ${j.title}');
    }
  });

  test('every journey has scenarios covering approve, review and reject', () {
    for (final j in kJourneys) {
      final outcomes = j.scenarios.map((s) => s.expected).toSet();
      expect(outcomes, containsAll(<String>['approve', 'review', 'reject']),
          reason: '${j.title} does not exercise all three decision paths');
    }
  });

  test('scenario values cover every declared field', () {
    for (final j in kJourneys) {
      final fieldKeys = j.fields.map((f) => f.key).toSet();
      for (final s in j.scenarios) {
        expect(s.values.keys.toSet(), equals(fieldKeys),
            reason: '${j.title} / ${s.name} field mismatch');
      }
    }
  });

  test('payloadFor nests values under the connector source id', () {
    final j = kJourneys.first;
    final p = j.payloadFor(j.scenarios.first.values);
    expect(p.keys, [j.sourceId]);
    expect((p[j.sourceId] as Map).length, j.fields.length);
  });
}
