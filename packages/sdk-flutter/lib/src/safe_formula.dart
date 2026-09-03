/// A bounded, allow-listing evaluator for author-supplied scorecard / metric formulas.
///
/// This is the Dart arm of `apps/python-executor/app/safe_formula.py` — the declared source of
/// truth. Like that module it parses the formula and evaluates ONLY an opted-in grammar:
/// arithmetic, comparison, boolean and conditional (`a if c else b`) expressions over names taken
/// from the caller's context, plus DIRECT calls to a fixed function table. There is no host
/// reflection, no member access, no indexing, no assignment and no way to name a callable that is
/// not in the table, so an author-supplied string cannot reach anything the SDK owns. Anything the
/// grammar does not opt into is refused with [FormulaError] before it runs — never best-effort.
///
/// Numbers keep Python's int/double distinction, which the scorecard contract depends on:
/// `3 / 2` is 1.5, `3 // 2` is 1, and `int(x)` truncates toward zero.
library;

import "dart:math" as math;

/// A formula was syntactically invalid, used a disallowed construct, or failed at runtime.
class FormulaError implements Exception {
  FormulaError(this.message);

  final String message;

  @override
  String toString() => message;
}

typedef FormulaFunction = Object? Function(List<Object?> args);

/// Guardrails: a formula is author-supplied and runs on someone's phone.
const int _maxSourceLength = 10000;
const int _maxDepth = 64;
const double _maxPowExponent = 1000;
const int _maxStringMultiply = 100000;

/// Evaluate [formula] over [context] (names) and [functions] (callables).
/// Throws [FormulaError] for a syntax error, a disallowed construct, an unknown name or a
/// function that failed on its inputs.
Object? evaluateFormula(
  String formula,
  Map<String, Object?> context, {
  Map<String, FormulaFunction>? functions,
}) {
  if (formula.length > _maxSourceLength) {
    throw FormulaError("formula too long");
  }
  final parser = _Parser(_Lexer(formula).tokenize());
  final node = parser.parseExpression(0);
  parser.expectEnd();
  return _Evaluator(context, functions ?? formulaFunctions).eval(node, 0);
}

// ── Truthiness, typing and Python arithmetic ────────────────────────────────────────────────

bool truthy(Object? value) {
  if (value == null) return false;
  if (value is bool) return value;
  if (value is num) return value != 0;
  if (value is String) return value.isNotEmpty;
  if (value is List) return value.isNotEmpty;
  if (value is Map) return value.isNotEmpty;
  return true;
}

String typeName(Object? value) {
  if (value == null) return "NoneType";
  if (value is bool) return "bool";
  if (value is int) return "int";
  if (value is double) return "float";
  if (value is String) return "str";
  if (value is List) return "list";
  if (value is Map) return "dict";
  return value.runtimeType.toString();
}

/// Python treats `bool` as an `int`; arithmetic promotes it before anything else.
Object _arithmetic(Object? value) {
  if (value is bool) return value ? 1 : 0;
  if (value is num) return value;
  if (value is String) return value;
  throw FormulaError("unsupported operand type: ${typeName(value)}");
}

num _numeric(Object? value) {
  final promoted = _arithmetic(value);
  if (promoted is String) throw FormulaError("unsupported operand type: str");
  return promoted as num;
}

double asDouble(Object? value) {
  if (value is bool) return value ? 1.0 : 0.0;
  if (value is num) return value.toDouble();
  throw FormulaError("expected a number, got ${typeName(value)}");
}

int asInt(Object? value) {
  if (value is bool) return value ? 1 : 0;
  if (value is num) return value.toInt();
  throw FormulaError("expected an integer, got ${typeName(value)}");
}

Object _add(Object? left, Object? right) {
  final a = _arithmetic(left);
  final b = _arithmetic(right);
  if (a is String && b is String) return a + b;
  if (a is String || b is String) {
    throw FormulaError("cannot add ${typeName(left)} and ${typeName(right)}");
  }
  return (a as num) + (b as num);
}

Object _subtract(Object? left, Object? right) => _numeric(left) - _numeric(right);

Object _multiply(Object? left, Object? right) {
  final a = _arithmetic(left);
  final b = _arithmetic(right);
  if (a is String || b is String) {
    final text = (a is String ? a : b) as String;
    final count = asInt(a is String ? b : a);
    if (count > _maxStringMultiply) throw FormulaError("string repetition too large");
    return count <= 0 ? "" : text * count;
  }
  return (a as num) * (b as num);
}

Object _divide(Object? left, Object? right) {
  final denominator = _numeric(right);
  if (denominator == 0) throw FormulaError("division by zero");
  return _numeric(left) / denominator;
}

Object _floorDivide(Object? left, Object? right) {
  final a = _numeric(left);
  final b = _numeric(right);
  if (b == 0) throw FormulaError("integer division or modulo by zero");
  // Python's `//` FLOORS; Dart's `~/` truncates toward zero, so -3 // 2 is -2, not -1.
  if (a is int && b is int) return (a / b).floor();
  return (a / b).floorToDouble();
}

Object _modulo(Object? left, Object? right) {
  final a = _numeric(left);
  final b = _numeric(right);
  if (b == 0) throw FormulaError("integer division or modulo by zero");
  // Python's `%` takes the sign of the DIVISOR. Dart's `%` is always non-negative, which differs
  // whenever the divisor is negative (Python: 5 % -3 == -1; Dart: 5 % -3 == 2).
  if (a is int && b is int) return a - (a / b).floor() * b;
  final x = a.toDouble();
  final y = b.toDouble();
  return x - (x / y).floorToDouble() * y;
}

Object _power(Object? left, Object? right) {
  final a = _numeric(left);
  final b = _numeric(right);
  if (b > _maxPowExponent) throw FormulaError("exponent too large");
  if (a is int && b is int && b >= 0) return math.pow(a, b).toInt();
  return math.pow(a.toDouble(), b.toDouble()).toDouble();
}

/// Python `==` — numeric across int/double/bool, exact otherwise. Never coerces strings.
bool pyEquals(Object? left, Object? right) {
  if (left == null || right == null) return left == null && right == null;
  final leftNumeric = left is num || left is bool;
  final rightNumeric = right is num || right is bool;
  if (leftNumeric && rightNumeric) return asDouble(left) == asDouble(right);
  if (leftNumeric != rightNumeric) return false;
  if (left is List && right is List) {
    if (left.length != right.length) return false;
    for (var i = 0; i < left.length; i += 1) {
      if (!pyEquals(left[i], right[i])) return false;
    }
    return true;
  }
  return left == right;
}

int pyCompare(Object? left, Object? right) {
  if (left is String && right is String) return left.compareTo(right);
  final leftNumeric = left is num || left is bool;
  final rightNumeric = right is num || right is bool;
  if (leftNumeric && rightNumeric) return asDouble(left).compareTo(asDouble(right));
  throw FormulaError("cannot order ${typeName(left)} and ${typeName(right)}");
}

// ── Lexer ──────────────────────────────────────────────────────────────────────────────────

enum _Kind { number, string, name, op, end }

class _Token {
  _Token(this.kind, this.text, [this.literal]);

  final _Kind kind;
  final String text;
  final Object? literal;
}

// Longest-first so `**` is not read as two `*`. Note what is ABSENT: `.` (attribute access),
// `=` (assignment / walrus), `{`, `:`, `;`, `@`, `|`, `&`, `~`, `^` — every one of those is a
// construct the Python evaluator also refuses.
const List<String> _operators = <String>[
  "**", "//", "==", "!=", "<=", ">=", "<", ">", "+", "-", "*", "/", "%", "(", ")", "[", "]", ",",
];

const Set<String> _reserved = <String>{"if", "else", "and", "or", "not", "in", "is", "for", "lambda", "await"};

class _Lexer {
  _Lexer(this.source);

  final String source;
  int index = 0;

  List<_Token> tokenize() {
    final tokens = <_Token>[];
    while (true) {
      while (index < source.length && _isSpace(source[index])) {
        index += 1;
      }
      if (index >= source.length) break;
      final ch = source[index];
      if (_isDigit(ch) || (ch == "." && index + 1 < source.length && _isDigit(source[index + 1]))) {
        tokens.add(_number());
      } else if (ch == '"' || ch == "'") {
        tokens.add(_string(ch));
      } else if (_isNameStart(ch)) {
        tokens.add(_name());
      } else {
        tokens.add(_operator());
      }
    }
    tokens.add(_Token(_Kind.end, ""));
    return tokens;
  }

  static bool _isSpace(String ch) => ch.trim().isEmpty;
  static bool _isDigit(String ch) => ch.codeUnitAt(0) >= 48 && ch.codeUnitAt(0) <= 57;
  static bool _isNameStart(String ch) {
    final code = ch.codeUnitAt(0);
    return ch == "_" || (code >= 65 && code <= 90) || (code >= 97 && code <= 122);
  }

  static bool _isNamePart(String ch) => _isNameStart(ch) || _isDigit(ch);

  _Token _number() {
    final start = index;
    var isFloat = false;
    while (index < source.length && (_isDigit(source[index]) || source[index] == "_")) {
      index += 1;
    }
    if (index < source.length && source[index] == ".") {
      isFloat = true;
      index += 1;
      while (index < source.length && (_isDigit(source[index]) || source[index] == "_")) {
        index += 1;
      }
    }
    if (index < source.length && (source[index] == "e" || source[index] == "E")) {
      final save = index;
      index += 1;
      if (index < source.length && (source[index] == "+" || source[index] == "-")) {
        index += 1;
      }
      if (index < source.length && _isDigit(source[index])) {
        isFloat = true;
        while (index < source.length && _isDigit(source[index])) {
          index += 1;
        }
      } else {
        index = save;
      }
    }
    final text = source.substring(start, index).replaceAll("_", "");
    final Object? literal = isFloat ? double.tryParse(text) : (int.tryParse(text) ?? double.tryParse(text));
    if (literal == null) throw FormulaError("invalid number literal: $text");
    return _Token(_Kind.number, text, literal);
  }

  _Token _string(String quote) {
    index += 1;
    final buffer = StringBuffer();
    while (true) {
      if (index >= source.length) throw FormulaError("invalid formula syntax: unterminated string");
      final ch = source[index];
      if (ch == quote) {
        index += 1;
        break;
      }
      if (ch == r"\") {
        index += 1;
        if (index >= source.length) throw FormulaError("invalid formula syntax: unterminated string");
        const escapes = <String, String>{"n": "\n", "t": "\t", "r": "\r"};
        buffer.write(escapes[source[index]] ?? source[index]);
        index += 1;
        continue;
      }
      buffer.write(ch);
      index += 1;
    }
    final text = buffer.toString();
    return _Token(_Kind.string, text, text);
  }

  _Token _name() {
    final start = index;
    while (index < source.length && _isNamePart(source[index])) {
      index += 1;
    }
    return _Token(_Kind.name, source.substring(start, index));
  }

  _Token _operator() {
    for (final candidate in _operators) {
      if (source.startsWith(candidate, index)) {
        index += candidate.length;
        return _Token(_Kind.op, candidate);
      }
    }
    throw FormulaError("disallowed character in formula: '${source[index]}'");
  }
}

// ── AST ────────────────────────────────────────────────────────────────────────────────────

abstract class _Node {}

class _Literal extends _Node {
  _Literal(this.value);
  final Object? value;
}

class _Name extends _Node {
  _Name(this.id);
  final String id;
}

class _Unary extends _Node {
  _Unary(this.op, this.operand);
  final String op;
  final _Node operand;
}

class _Binary extends _Node {
  _Binary(this.op, this.left, this.right);
  final String op;
  final _Node left;
  final _Node right;
}

class _BoolOp extends _Node {
  _BoolOp(this.op, this.values);
  final String op;
  final List<_Node> values;
}

class _Compare extends _Node {
  _Compare(this.left, this.ops, this.comparators);
  final _Node left;
  final List<String> ops;
  final List<_Node> comparators;
}

class _Conditional extends _Node {
  _Conditional(this.test, this.body, this.orelse);
  final _Node test;
  final _Node body;
  final _Node orelse;
}

class _Call extends _Node {
  _Call(this.name, this.args);
  final String name;
  final List<_Node> args;
}

class _Sequence extends _Node {
  _Sequence(this.elements);
  final List<_Node> elements;
}

// ── Parser (precedence exactly as CPython's grammar) ────────────────────────────────────────

class _Parser {
  _Parser(this.tokens);

  final List<_Token> tokens;
  int position = 0;

  _Token get _peek => tokens[position];

  bool _atOp(List<String> candidates) => _peek.kind == _Kind.op && candidates.contains(_peek.text);

  bool _atKeyword(String keyword) => _peek.kind == _Kind.name && _peek.text == keyword;

  _Token _advance() => tokens[position++];

  void _expectOp(String op) {
    if (!_atOp(<String>[op])) throw FormulaError("invalid formula syntax: expected '$op'");
    position += 1;
  }

  void expectEnd() {
    if (_peek.kind != _Kind.end) {
      throw FormulaError("invalid formula syntax: unexpected '${_peek.text}'");
    }
  }

  void _guard(int depth) {
    if (depth > _maxDepth) throw FormulaError("formula nesting too deep");
  }

  _Node parseExpression(int depth) {
    _guard(depth);
    final body = _parseOr(depth + 1);
    if (_atKeyword("if")) {
      _advance();
      final test = _parseOr(depth + 1);
      if (!_atKeyword("else")) throw FormulaError("invalid formula syntax: conditional needs 'else'");
      _advance();
      return _Conditional(test, body, parseExpression(depth + 1));
    }
    return body;
  }

  _Node _parseOr(int depth) {
    _guard(depth);
    final node = _parseAnd(depth + 1);
    if (!_atKeyword("or")) return node;
    final values = <_Node>[node];
    while (_atKeyword("or")) {
      _advance();
      values.add(_parseAnd(depth + 1));
    }
    return _BoolOp("or", values);
  }

  _Node _parseAnd(int depth) {
    _guard(depth);
    final node = _parseNot(depth + 1);
    if (!_atKeyword("and")) return node;
    final values = <_Node>[node];
    while (_atKeyword("and")) {
      _advance();
      values.add(_parseNot(depth + 1));
    }
    return _BoolOp("and", values);
  }

  _Node _parseNot(int depth) {
    _guard(depth);
    if (_atKeyword("not")) {
      _advance();
      return _Unary("not", _parseNot(depth + 1));
    }
    return _parseComparison(depth + 1);
  }

  _Node _parseComparison(int depth) {
    _guard(depth);
    final left = _parseArithmetic(depth + 1);
    final ops = <String>[];
    final comparators = <_Node>[];
    while (_atOp(const <String>["==", "!=", "<", "<=", ">", ">="])) {
      ops.add(_advance().text);
      comparators.add(_parseArithmetic(depth + 1));
    }
    // `in` and `is` are deliberately unreachable: the Python evaluator has no handler for them
    // either, and silently treating them as anything would diverge.
    if (_atKeyword("in") || _atKeyword("is")) {
      throw FormulaError("disallowed comparison");
    }
    return ops.isEmpty ? left : _Compare(left, ops, comparators);
  }

  _Node _parseArithmetic(int depth) {
    _guard(depth);
    var node = _parseTerm(depth + 1);
    while (_atOp(const <String>["+", "-"])) {
      final op = _advance().text;
      node = _Binary(op, node, _parseTerm(depth + 1));
    }
    return node;
  }

  _Node _parseTerm(int depth) {
    _guard(depth);
    var node = _parseUnary(depth + 1);
    while (_atOp(const <String>["*", "/", "//", "%"])) {
      final op = _advance().text;
      node = _Binary(op, node, _parseUnary(depth + 1));
    }
    return node;
  }

  _Node _parseUnary(int depth) {
    _guard(depth);
    if (_atOp(const <String>["+", "-"])) {
      final op = _advance().text;
      return _Unary(op, _parseUnary(depth + 1));
    }
    return _parsePower(depth + 1);
  }

  _Node _parsePower(int depth) {
    _guard(depth);
    final base = _parseAtom(depth + 1);
    if (_atOp(const <String>["**"])) {
      _advance();
      // Right-associative, and its right operand may carry a unary sign (`2 ** -1`).
      return _Binary("**", base, _parseUnary(depth + 1));
    }
    return base;
  }

  _Node _parseAtom(int depth) {
    _guard(depth);
    final token = _peek;
    switch (token.kind) {
      case _Kind.number:
      case _Kind.string:
        _advance();
        return _Literal(token.literal);
      case _Kind.name:
        if (_reserved.contains(token.text)) {
          throw FormulaError("invalid formula syntax: unexpected '${token.text}'");
        }
        _advance();
        if (_atOp(const <String>["("])) {
          _advance();
          final args = <_Node>[];
          if (!_atOp(const <String>[")"])) {
            args.add(parseExpression(depth + 1));
            while (_atOp(const <String>[","])) {
              _advance();
              if (_atOp(const <String>[")"])) break;
              args.add(parseExpression(depth + 1));
            }
          }
          _expectOp(")");
          // Only a DIRECT call to a bare name. `f()()`, `obj.method()` and `table[x]()` can never
          // be spelled, because there is no postfix call/attribute/subscript production at all.
          return _Call(token.text, args);
        }
        return _Name(token.text);
      case _Kind.op:
        if (token.text == "(") {
          _advance();
          if (_atOp(const <String>[")"])) {
            _advance();
            return _Sequence(const <_Node>[]);
          }
          final first = parseExpression(depth + 1);
          if (_atOp(const <String>[","])) {
            final elements = <_Node>[first];
            while (_atOp(const <String>[","])) {
              _advance();
              if (_atOp(const <String>[")"])) break;
              elements.add(parseExpression(depth + 1));
            }
            _expectOp(")");
            return _Sequence(elements);
          }
          _expectOp(")");
          return first;
        }
        if (token.text == "[") {
          _advance();
          final elements = <_Node>[];
          if (!_atOp(const <String>["]"])) {
            elements.add(parseExpression(depth + 1));
            while (_atOp(const <String>[","])) {
              _advance();
              if (_atOp(const <String>["]"])) break;
              elements.add(parseExpression(depth + 1));
            }
          }
          _expectOp("]");
          return _Sequence(elements);
        }
        throw FormulaError("invalid formula syntax: unexpected '${token.text}'");
      case _Kind.end:
        throw FormulaError("invalid formula syntax: unexpected end of formula");
    }
  }
}

// ── Evaluator ──────────────────────────────────────────────────────────────────────────────

class _Evaluator {
  _Evaluator(this.context, this.functions);

  final Map<String, Object?> context;
  final Map<String, FormulaFunction> functions;

  Object? eval(_Node node, int depth) {
    if (depth > _maxDepth) throw FormulaError("formula nesting too deep");
    if (node is _Literal) return node.value;
    if (node is _Name) {
      if (!context.containsKey(node.id)) throw FormulaError("unknown name: ${node.id}");
      return context[node.id];
    }
    if (node is _Unary) {
      final operand = eval(node.operand, depth + 1);
      switch (node.op) {
        case "not":
          return !truthy(operand);
        case "-":
          return -_numeric(operand);
        default:
          return _numeric(operand);
      }
    }
    if (node is _Binary) {
      final left = eval(node.left, depth + 1);
      final right = eval(node.right, depth + 1);
      switch (node.op) {
        case "+":
          return _add(left, right);
        case "-":
          return _subtract(left, right);
        case "*":
          return _multiply(left, right);
        case "/":
          return _divide(left, right);
        case "//":
          return _floorDivide(left, right);
        case "%":
          return _modulo(left, right);
        case "**":
          return _power(left, right);
        default:
          throw FormulaError("disallowed operator: ${node.op}");
      }
    }
    if (node is _BoolOp) {
      // The Python evaluator materialises every operand before selecting, so an unknown name on
      // the right of a short-circuit still raises. Match that.
      final values = node.values.map((item) => eval(item, depth + 1)).toList();
      if (node.op == "and") {
        for (final value in values) {
          if (!truthy(value)) return value;
        }
        return values.last;
      }
      for (final value in values) {
        if (truthy(value)) return value;
      }
      return values.last;
    }
    if (node is _Compare) {
      var left = eval(node.left, depth + 1);
      for (var i = 0; i < node.ops.length; i += 1) {
        final right = eval(node.comparators[i], depth + 1);
        final bool passed;
        switch (node.ops[i]) {
          case "==":
            passed = pyEquals(left, right);
          case "!=":
            passed = !pyEquals(left, right);
          case "<":
            passed = pyCompare(left, right) < 0;
          case "<=":
            passed = pyCompare(left, right) <= 0;
          case ">":
            passed = pyCompare(left, right) > 0;
          case ">=":
            passed = pyCompare(left, right) >= 0;
          default:
            throw FormulaError("disallowed comparison");
        }
        if (!passed) return false;
        left = right;
      }
      return true;
    }
    if (node is _Conditional) {
      return truthy(eval(node.test, depth + 1)) ? eval(node.body, depth + 1) : eval(node.orelse, depth + 1);
    }
    if (node is _Call) {
      final function = functions[node.name];
      if (function == null) throw FormulaError("unknown function: ${node.name}");
      final args = node.args.map((item) => eval(item, depth + 1)).toList();
      try {
        return function(args);
      } on FormulaError {
        rethrow;
      } catch (error) {
        throw FormulaError("${node.name} failed: $error");
      }
    }
    if (node is _Sequence) {
      return node.elements.map((item) => eval(item, depth + 1)).toList();
    }
    throw FormulaError("disallowed expression");
  }
}

// ── The function table ─────────────────────────────────────────────────────────────────────

/// Python's `round()` — round-half-to-EVEN, not half-away-from-zero. The WoE score runs through
/// this, so getting it wrong shifts a credit score by a point at every .5 boundary.
double rintHalfToEven(double value) {
  if (value.isNaN || value.isInfinite) return value;
  final floor = value.floorToDouble();
  final fraction = value - floor;
  if (fraction > 0.5) return floor + 1;
  if (fraction < 0.5) return floor;
  return floor % 2 == 0 ? floor : floor + 1;
}

int bankersRound(double value) {
  if (value.isNaN || value.isInfinite) throw FormulaError("cannot round $value");
  return rintHalfToEven(value).toInt();
}

double _bankersRoundDigits(double value, int digits) {
  if (value.isNaN || value.isInfinite) return value;
  final scale = math.pow(10, digits).toDouble();
  if (scale == 0 || scale.isInfinite) return value;
  return rintHalfToEven(value * scale) / scale;
}

int _pyTruncate(double value) {
  if (value.isNaN || value.isInfinite) throw FormulaError("cannot convert $value to an integer");
  return value.truncate();
}

/// `_num` in excel_functions.py: coerce to double, falling back to [fallback].
double _num(Object? value, [double fallback = 0.0]) {
  if (value is bool) return value ? 1.0 : 0.0;
  if (value is num) return value.toDouble();
  if (value is String) return double.tryParse(value.trim()) ?? fallback;
  return fallback;
}

/// `_flat`: flatten nested lists, preserving element types.
List<Object?> _flat(List<Object?> args) {
  final out = <Object?>[];
  for (final arg in args) {
    if (arg is List) {
      out.addAll(_flat(arg.cast<Object?>()));
    } else {
      out.add(arg);
    }
  }
  return out;
}

/// `_nums`: flatten, coerce to double, DROP anything non-numeric (including unparsable strings).
List<double> _nums(List<Object?> args) {
  final out = <double>[];
  for (final arg in args) {
    if (arg is List) {
      out.addAll(_nums(arg.cast<Object?>()));
    } else if (arg is bool) {
      out.add(arg ? 1.0 : 0.0);
    } else if (arg is num) {
      out.add(arg.toDouble());
    } else if (arg is String) {
      final parsed = double.tryParse(arg.trim());
      if (parsed != null) out.add(parsed);
    }
  }
  return out;
}

Object? _arg(List<Object?> args, int index, [Object? fallback]) => index < args.length ? args[index] : fallback;

void _require(List<Object?> args, int count, String name) {
  if (args.length < count) throw FormulaError("$name expects at least $count argument(s)");
}

Object? _extremum(List<Object?> args, {required bool wantMax}) {
  final items = args.length == 1 && args.first is List ? (args.first! as List).cast<Object?>() : args;
  if (items.isEmpty) throw FormulaError("${wantMax ? "max" : "min"} failed: empty sequence");
  var best = items.first;
  for (final item in items.skip(1)) {
    final ordering = pyCompare(item, best);
    if (wantMax ? ordering > 0 : ordering < 0) best = item;
  }
  return best;
}

/// Python's `str()` for the value kinds a formula can produce.
String _pyStr(Object? value) {
  if (value == null) return "None";
  if (value is bool) return value ? "True" : "False";
  if (value is double) {
    if (value.isNaN) return "nan";
    if (value.isInfinite) return value > 0 ? "inf" : "-inf";
    if (value == value.floorToDouble() && value.abs() < 1e16) return "${value.toInt()}.0";
    return value.toString();
  }
  return value.toString();
}

/// The formula function table. This is the Dart subset of the Python core's
/// `EXCEL_FUNCTIONS + _FORMULA_BUILTINS`: the numeric, statistical and logical functions a
/// scorecard or metric formula realistically calls, ported with the Python implementations' exact
/// return types (`ABS` is a float, `SIGN` is an int, `INT` floors, `TRUNC` truncates).
///
/// The rest of the ~230-entry Excel table is NOT ported. A formula that calls one of those raises
/// `unknown function: NAME` — loud and identical in shape to Python's own message for a name it
/// does not know — rather than silently producing a different score.
final Map<String, FormulaFunction> formulaFunctions = <String, FormulaFunction>{
  // ── Math ──
  "ABS": (args) {
    _require(args, 1, "ABS");
    return _num(args[0]).abs();
  },
  "SQRT": (args) {
    _require(args, 1, "SQRT");
    final x = _num(args[0]);
    if (x < 0) throw FormulaError("math domain error");
    return math.sqrt(x);
  },
  "POWER": (args) {
    _require(args, 2, "POWER");
    return math.pow(_num(args[0]), _num(args[1])).toDouble();
  },
  "EXP": (args) {
    _require(args, 1, "EXP");
    return math.exp(_num(args[0]));
  },
  "LN": (args) {
    _require(args, 1, "LN");
    final x = _num(args[0]);
    if (x <= 0) throw FormulaError("math domain error");
    return math.log(x);
  },
  "LOG": (args) {
    _require(args, 1, "LOG");
    final x = _num(args[0]);
    final base = _num(_arg(args, 1, 10.0), 10.0);
    if (x <= 0 || base <= 0 || base == 1) throw FormulaError("math domain error");
    return math.log(x) / math.log(base);
  },
  "LOG10": (args) {
    _require(args, 1, "LOG10");
    final x = _num(args[0]);
    if (x <= 0) throw FormulaError("math domain error");
    return math.log(x) / math.ln10;
  },
  "MOD": (args) {
    _require(args, 2, "MOD");
    final divisor = _num(args[1]);
    if (divisor == 0) throw FormulaError("MOD: divisor cannot be zero");
    final n = _num(args[0]);
    return n - (n / divisor).floorToDouble() * divisor;
  },
  "SIGN": (args) {
    _require(args, 1, "SIGN");
    final n = _num(args[0]);
    return n > 0 ? 1 : (n < 0 ? -1 : 0);
  },
  "INT": (args) {
    _require(args, 1, "INT");
    return _num(args[0]).floor();
  },
  "TRUNC": (args) {
    _require(args, 1, "TRUNC");
    final scale = math.pow(10, asInt(_arg(args, 1, 0) ?? 0)).toDouble();
    return _pyTruncate(_num(args[0]) * scale) / scale;
  },
  "ROUND": (args) {
    _require(args, 1, "ROUND");
    return _bankersRoundDigits(_num(args[0]), asInt(_arg(args, 1, 0) ?? 0));
  },
  "ROUNDUP": (args) {
    _require(args, 1, "ROUNDUP");
    final scale = math.pow(10, asInt(_arg(args, 1, 0) ?? 0)).toDouble();
    final n = _num(args[0]);
    return n >= 0 ? (n * scale).ceilToDouble() / scale : (n * scale).floorToDouble() / scale;
  },
  "ROUNDDOWN": (args) {
    _require(args, 1, "ROUNDDOWN");
    final scale = math.pow(10, asInt(_arg(args, 1, 0) ?? 0)).toDouble();
    return (_num(args[0]) * scale).truncateToDouble() / scale;
  },
  "CEILING": (args) {
    _require(args, 1, "CEILING");
    final significance = _num(_arg(args, 1, 1.0), 1.0);
    if (significance == 0) return 0.0;
    return (_num(args[0]) / significance).ceilToDouble() * significance;
  },
  "FLOOR": (args) {
    _require(args, 1, "FLOOR");
    final significance = _num(_arg(args, 1, 1.0), 1.0);
    if (significance == 0) return 0.0;
    return (_num(args[0]) / significance).floorToDouble() * significance;
  },
  "PI": (args) => math.pi,
  "SUM": (args) {
    final values = _nums(args);
    // `sum([])` is the integer 0 in Python, not 0.0.
    return values.isEmpty ? 0 : values.reduce((a, b) => a + b);
  },
  "PRODUCT": (args) => _nums(args).fold<double>(1.0, (acc, value) => acc * value),

  // ── Statistical ──
  "MIN": (args) {
    final values = _nums(args);
    return values.isEmpty ? 0.0 : values.reduce(math.min);
  },
  "MAX": (args) {
    final values = _nums(args);
    return values.isEmpty ? 0.0 : values.reduce(math.max);
  },
  "AVERAGE": (args) {
    final values = _nums(args);
    if (values.isEmpty) throw FormulaError("AVERAGE: no numeric values");
    return values.reduce((a, b) => a + b) / values.length;
  },
  "MEDIAN": (args) {
    final values = _nums(args)..sort();
    if (values.isEmpty) throw FormulaError("MEDIAN: no values");
    final middle = values.length ~/ 2;
    return values.length.isOdd ? values[middle] : (values[middle - 1] + values[middle]) / 2.0;
  },
  "COUNT": (args) => _nums(args).length,

  // ── Logical ──
  "AND": (args) => _flat(args).every(truthy),
  "OR": (args) => _flat(args).any(truthy),
  "NOT": (args) {
    _require(args, 1, "NOT");
    return !truthy(args[0]);
  },
  "XOR": (args) => _flat(args).where(truthy).length.isOdd,
  "IF": (args) {
    _require(args, 1, "IF");
    return truthy(args[0]) ? _arg(args, 1, true) : _arg(args, 2, false);
  },
  "IFS": (args) {
    for (var i = 0; i < args.length; i += 2) {
      if (truthy(args[i]) && i + 1 < args.length) return args[i + 1];
    }
    throw FormulaError("IFS: no condition is TRUE");
  },
  "TRUE": (args) => true,
  "FALSE": (args) => false,

  // ── Plain builtins the Python table also exposes (lowercase, so no collision above) ──
  "abs": (args) {
    _require(args, 1, "abs");
    final value = _arithmetic(args[0]);
    if (value is num) return value.abs();
    throw FormulaError("abs failed: bad operand type");
  },
  "min": (args) => _extremum(args, wantMax: false),
  "max": (args) => _extremum(args, wantMax: true),
  "round": (args) {
    _require(args, 1, "round");
    if (args.length >= 2) return _bankersRoundDigits(asDouble(args[0]), asInt(args[1]));
    return bankersRound(asDouble(args[0]));
  },
  "int": (args) {
    _require(args, 1, "int");
    final value = args[0];
    if (value is bool) return value ? 1 : 0;
    if (value is num) return _pyTruncate(value.toDouble());
    if (value is String) {
      final parsed = int.tryParse(value.trim());
      if (parsed == null) throw FormulaError("int failed: invalid literal for int(): '$value'");
      return parsed;
    }
    throw FormulaError("int failed: bad operand type");
  },
  "float": (args) {
    _require(args, 1, "float");
    final value = args[0];
    if (value is bool) return value ? 1.0 : 0.0;
    if (value is num) return value.toDouble();
    if (value is String) {
      final parsed = double.tryParse(value.trim());
      if (parsed == null) throw FormulaError("float failed: could not convert string to float: '$value'");
      return parsed;
    }
    throw FormulaError("float failed: bad operand type");
  },
  "str": (args) {
    _require(args, 1, "str");
    return _pyStr(args[0]);
  },
  "bool": (args) {
    _require(args, 1, "bool");
    return truthy(args[0]);
  },
  "len": (args) {
    _require(args, 1, "len");
    final value = args[0];
    if (value is String) return value.length;
    if (value is List) return value.length;
    if (value is Map) return value.length;
    throw FormulaError("len failed: object of type ${typeName(value)} has no len()");
  },
  "sum": (args) {
    _require(args, 1, "sum");
    final value = args[0];
    if (value is! List) throw FormulaError("sum failed: ${typeName(value)} is not iterable");
    Object total = args.length >= 2 ? _arithmetic(args[1]) : 0;
    for (final item in value) {
      total = _add(total, item);
    }
    return total;
  },
};
