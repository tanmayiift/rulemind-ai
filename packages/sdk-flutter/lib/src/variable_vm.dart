import 'models.dart';

class VariableVm {
  dynamic evaluate(CompiledVariable variable, Map<String, dynamic> payload, Map<String, dynamic> variables) {
    final sourcePayload =
        payload[variable.sourceId] is Map<String, dynamic> ? payload[variable.sourceId] : payload;
    // `payload` must be the variable's SOURCE-SCOPED payload, because that is what
    // the compiler means by {"op":"get","source":"payload"} — it mirrors the Python
    // authoring contract, where @variable(source: "claim") receives only the claim
    // dict. Binding the FULL payload here made every connector-sourced variable
    // read its default (0) whenever the caller sent the documented source-keyed
    // shape {"claim": {...}}, so on-device decisions silently diverged from the
    // server. The whole payload stays reachable as `rootPayload`.
    final registers = <String, dynamic>{
      'payload': sourcePayload,
      'rootPayload': payload,
      'variables': variables,
      'context': variables,
      'source': sourcePayload,
    };

    for (final instruction in variable.instructions) {
      final op = instruction.op.toLowerCase();
      final target = instruction.target;
      switch (op) {
        case 'literal':
          if (target != null) {
            registers[target] = instruction.value;
          }
          break;
        case 'get':
          if (target != null) {
            final source = _resolveRegister(instruction.source, registers) ?? registers['source'];
            // The compiler passes the lookup key and the default THROUGH REGISTERS:
            //   {"op":"literal","target":"r1","value":"claim_amount_inr"}
            //   {"op":"get","source":"payload","key":"r1","default":"r2",...}
            // Reading "r1" as a literal field name made every compiled variable miss
            // its field and fall back to the default — which, read literally, was the
            // string "r2" and cast to 0. Every on-device variable resolved to 0.
            final path = _deref(instruction.path ?? instruction.key, registers);
            final fallback = _deref(instruction.defaultValue, registers);
            // Two path conventions are in use: the compiler emits a bare field name
            // relative to the variable's source payload, while hand-authored bundles
            // use a rooted "sourceId.field". Try the scoped payload, then the root,
            // so both resolve without the caller having to pick one.
            registers[target] =
                _resolvePath(source, path) ?? _resolvePath(registers['rootPayload'], path) ?? fallback;
          }
          break;
        case 'filter':
          if (target != null) {
            final source = _asList(_resolveRegister(instruction.source, registers));
            registers[target] = source
                .where((item) => _evaluatePredicate(
                      item,
                      instruction.predicatePath ?? instruction.path,
                      instruction.predicateOperator,
                      instruction.predicateValue ?? instruction.value,
                    ))
                .toList();
          }
          break;
        case 'find':
          if (target != null) {
            final source = _asList(_resolveRegister(instruction.source, registers));
            registers[target] = source.cast<dynamic>().firstWhere(
                  (item) => _evaluatePredicate(
                    item,
                    instruction.predicatePath ?? instruction.path,
                    instruction.predicateOperator,
                    instruction.predicateValue ?? instruction.value,
                  ),
                  orElse: () => null,
                );
          }
          break;
        case 'sum':
          if (target != null) {
            registers[target] = _extractNumericValues(instruction, registers).fold<double>(0, (sum, value) => sum + value);
          }
          break;
        case 'count':
          if (target != null) {
            final value = _resolveRegister(instruction.source, registers);
            if (value is List) {
              registers[target] = value.length;
            } else if (value is Map) {
              registers[target] = value.length;
            } else if (value == null) {
              registers[target] = 0;
            } else {
              registers[target] = 1;
            }
          }
          break;
        case 'count_where':
          if (target != null) {
            final source = _asList(_resolveRegister(instruction.source, registers));
            registers[target] = source
                .where((item) => _evaluatePredicate(
                      item,
                      instruction.predicatePath ?? instruction.path,
                      instruction.predicateOperator,
                      instruction.predicateValue ?? instruction.value,
                    ))
                .length;
          }
          break;
        case 'max':
          if (target != null) {
            final values = _extractNumericValues(instruction, registers);
            registers[target] = values.isEmpty ? 0.0 : values.reduce((left, right) => left > right ? left : right);
          }
          break;
        case 'min':
          if (target != null) {
            final values = _extractNumericValues(instruction, registers);
            registers[target] = values.isEmpty ? 0.0 : values.reduce((left, right) => left < right ? left : right);
          }
          break;
        case 'divide':
        case 'multiply':
        case 'add':
        case 'subtract':
          if (target != null) {
            final left = _numeric(_resolveOperand(instruction.left, registers, instruction.values.isNotEmpty ? instruction.values.first : null));
            final right = _numeric(
              _resolveOperand(
                instruction.right,
                registers,
                instruction.values.length > 1 ? instruction.values[1] : instruction.value ?? instruction.defaultValue,
              ),
            );
            registers[target] = switch (op) {
              'divide' => right == 0 ? 0.0 : left / right,
              'multiply' => left * right,
              'subtract' => left - right,
              _ => left + right,
            };
          }
          break;
        case 'cast':
          if (target != null) {
            registers[target] = _castValue(_resolveRegister(instruction.source, registers), instruction.type);
          }
          break;
        case 'compare':
          if (target != null) {
            registers[target] = _compare(
              _resolveOperand(instruction.left, registers, null),
              instruction.predicateOperator ?? instruction.type ?? '==',
              _resolveOperand(instruction.right, registers, instruction.value),
            );
          }
          break;
        case 'if':
          if (target != null) {
            final condition = _resolveRegister(instruction.source, registers);
            final branch = condition is bool ? condition : _numeric(condition) != 0;
            registers[target] = branch
                ? _resolveOperand(instruction.thenSource, registers, instruction.thenValue)
                : _resolveOperand(instruction.elseSource, registers, instruction.elseValue);
          }
          break;
        case 'return':
          return _resolveOperand(instruction.source ?? target, registers, instruction.value);
      }
    }
    return null;
  }

  /// Compiler-allocated registers are named `r<n>`. When an instruction field holds
  /// such a name, it is a reference to a previously computed value rather than a
  /// literal. Anything else is returned untouched.
  static final RegExp _registerName = RegExp(r'^r\d+$');

  dynamic _deref(dynamic token, Map<String, dynamic> registers) {
    if (token is String && _registerName.hasMatch(token) && registers.containsKey(token)) {
      return registers[token];
    }
    return token;
  }

  dynamic _resolveRegister(String? name, Map<String, dynamic> registers) {
    if (name == null || name.isEmpty) {
      return null;
    }
    if (registers.containsKey(name)) {
      return registers[name];
    }
    return _resolvePath(registers['source'], name) ?? _resolvePath(registers['payload'], name) ?? _resolvePath(registers['variables'], name);
  }

  dynamic _resolveOperand(String? name, Map<String, dynamic> registers, dynamic fallback) => _resolveRegister(name, registers) ?? fallback;

  List<double> _extractNumericValues(Instruction instruction, Map<String, dynamic> registers) {
    final directSources = instruction.sources.isNotEmpty
        ? instruction.sources
        : <String>[
            if (instruction.source != null && instruction.source!.isNotEmpty) instruction.source!,
            ...instruction.args,
          ];
    if (directSources.isNotEmpty) {
      return directSources.map((source) => _numeric(_resolveRegister(source, registers))).toList();
    }
    final list = _asList(_resolveRegister(instruction.source, registers));
    return list
        .map((item) => instruction.path == null || instruction.path!.isEmpty ? item : _resolvePath(item, instruction.path))
        .map(_numeric)
        .toList();
  }

  List<dynamic> _asList(dynamic value) {
    if (value == null) {
      return const <dynamic>[];
    }
    if (value is List<dynamic>) {
      return value;
    }
    return <dynamic>[value];
  }

  dynamic _resolvePath(dynamic root, String? rawPath) {
    if (root == null || rawPath == null || rawPath.isEmpty) {
      return root;
    }
    final path = rawPath.startsWith(r'$.') ? rawPath.substring(2) : rawPath;
    if (path.isEmpty) {
      return root;
    }
    dynamic current = root;
    for (final segment in path.split('.')) {
      if (current == null) {
        return null;
      }
      final match = RegExp(r'([A-Za-z0-9_\-]+)(\[(\-?\d+)\])?').firstMatch(segment);
      if (match == null) {
        return null;
      }
      final key = match.group(1)!;
      current = current is Map<String, dynamic> ? current[key] : null;
      final indexGroup = match.group(3);
      if (indexGroup != null) {
        final list = current is List<dynamic> ? current : null;
        if (list == null) {
          return null;
        }
        final requested = int.parse(indexGroup);
        final resolvedIndex = requested < 0 ? list.length + requested : requested;
        current = resolvedIndex >= 0 && resolvedIndex < list.length ? list[resolvedIndex] : null;
      }
    }
    return current;
  }

  bool _evaluatePredicate(dynamic item, String? path, String? operator, dynamic expected) {
    final actual = path == null || path.isEmpty ? item : _resolvePath(item, path);
    return _compare(actual, operator ?? '==', expected);
  }

  bool _compare(dynamic actual, String operator, dynamic expected) {
    switch (operator) {
      case '>':
        return _numeric(actual) > _numeric(expected);
      case '>=':
        return _numeric(actual) >= _numeric(expected);
      case '<':
        return _numeric(actual) < _numeric(expected);
      case '<=':
        return _numeric(actual) <= _numeric(expected);
      case '!=':
        return _normalize(actual) != _normalize(expected);
      case 'in':
        return _asList(expected).map(_normalize).contains(_normalize(actual));
      case 'not_in':
        return !_asList(expected).map(_normalize).contains(_normalize(actual));
      default:
        return _normalize(actual) == _normalize(expected);
    }
  }

  dynamic _castValue(dynamic value, String? type) {
    switch ((type ?? 'string').toLowerCase()) {
      case 'int':
      case 'integer':
        return _numeric(value).toInt();
      case 'float':
      case 'double':
      case 'number':
        return _numeric(value);
      case 'bool':
      case 'boolean':
        if (value is bool) {
          return value;
        }
        if (value is num) {
          return value != 0;
        }
        if (value is String) {
          return value.toLowerCase() == 'true' || value == '1';
        }
        return value != null;
      case 'list':
        return _asList(value);
      default:
        return value?.toString();
    }
  }

  dynamic _normalize(dynamic value) {
    if (value is num) {
      return value.toDouble();
    }
    if (value is String) {
      return value.trim();
    }
    return value;
  }

  double _numeric(dynamic value) {
    if (value is num) {
      return value.toDouble();
    }
    if (value is bool) {
      return value ? 1.0 : 0.0;
    }
    if (value is String) {
      return double.tryParse(value) ?? 0.0;
    }
    return 0.0;
  }
}
