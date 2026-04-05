import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:rulemind_example/main.dart';

void main() {
  testWidgets('App launches smoke test', (WidgetTester tester) async {
    await tester.pumpWidget(const RuleMindStudioApp());
    // Verify the app renders without crashing
    expect(find.byType(MaterialApp), findsOneWidget);
  });
}
