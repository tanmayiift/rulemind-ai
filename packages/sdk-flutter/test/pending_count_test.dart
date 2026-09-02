import 'package:flutter_test/flutter_test.dart';
import 'package:rulemind/rulemind.dart';

void main() {
  test('pendingDecisionCount is safe before initialize', () async {
    await RuleMind.resetForTest();
    expect(await RuleMind.pendingDecisionCount(), 0);
  });
}
