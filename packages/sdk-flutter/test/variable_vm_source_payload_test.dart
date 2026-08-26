import 'package:flutter_test/flutter_test.dart';
import 'package:rulemind/src/models.dart';
import 'package:rulemind/src/variable_vm.dart';

/// The exact instruction shape the server compiler emits for
///   @variable(source="claim")
///   def claim_amount_inr(payload, variables, apis):
///       return int(payload.get("claim_amount_inr", 0))
CompiledVariable _claimAmount() => CompiledVariable.fromJson({
      "id": "claim_amount_inr",
      "sourceId": "claim",
      "name": "Claim Amount (INR)",
      "instructions": [
        {"op": "literal", "target": "r1", "value": "claim_amount_inr"},
        {"op": "literal", "target": "r2", "value": 0},
        {"op": "get", "source": "payload", "key": "r1", "default": "r2", "target": "r3"},
        {"op": "cast", "source": "r3", "target": "r4", "type": "int"},
        {"op": "return", "source": "r4"},
      ],
    });

void main() {
  final vm = VariableVm();

  test('source-keyed payload resolves through the variable source id', () {
    final value = vm.evaluate(_claimAmount(), {
      "claim": {"claim_amount_inr": 28000},
    }, <String, dynamic>{});
    expect(value, 28000,
        reason: 'the documented source-keyed shape must reach the variable');
  });

  test('flat payload still resolves', () {
    final value = vm.evaluate(_claimAmount(), {"claim_amount_inr": 28000}, <String, dynamic>{});
    expect(value, 28000);
  });

  test('missing field falls back to the compiled default', () {
    final value = vm.evaluate(_claimAmount(), {"claim": <String, dynamic>{}}, <String, dynamic>{});
    expect(value, 0);
  });

  test('an unrelated source does not leak into the variable', () {
    final value = vm.evaluate(_claimAmount(), {
      "checkout": {"claim_amount_inr": 999},
    }, <String, dynamic>{});
    expect(value, 0, reason: 'claim variable must not read the checkout payload');
  });
}
