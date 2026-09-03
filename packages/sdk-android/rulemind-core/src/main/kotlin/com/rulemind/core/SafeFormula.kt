package com.rulemind.core

import kotlin.math.abs
import kotlin.math.ceil
import kotlin.math.exp
import kotlin.math.floor
import kotlin.math.ln
import kotlin.math.log10
import kotlin.math.pow
import kotlin.math.sqrt
import kotlin.math.truncate

/**
 * A bounded, allow-listing evaluator for author-supplied scorecard / metric formulas.
 *
 * This is the Kotlin arm of `apps/python-executor/app/safe_formula.py` — the declared source of
 * truth. Like that module it parses the formula and evaluates ONLY an opted-in grammar:
 * arithmetic, comparison, boolean and conditional (`a if c else b`) expressions over names taken
 * from the caller's context, plus DIRECT calls to a fixed function table. There is no host
 * reflection, no member access, no indexing, no assignment and no way to name a callable that is
 * not in the table, so an author-supplied string cannot reach anything the SDK owns. Anything the
 * grammar does not opt into is refused with [FormulaError] before it runs — never best-effort.
 *
 * Numbers carry Python's int/float distinction (`Long` vs `Double`) because the scorecard contract
 * depends on it: `3 / 2` is 1.5, `3 // 2` is 1, and `int(x)` truncates toward zero.
 */
class FormulaError(message: String) : Exception(message)

object SafeFormula {
    /** Guardrails: a formula is author-supplied and runs on someone's phone. */
    private const val MAX_SOURCE_LENGTH = 10_000
    private const val MAX_DEPTH = 64
    private const val MAX_POW_EXPONENT = 1000.0
    private const val MAX_STRING_MULTIPLY = 100_000L

    /**
     * Evaluate [formula] over [context] (names) and [functions] (callables).
     * Throws [FormulaError] for a syntax error, a disallowed construct, an unknown name or a
     * function that failed on its inputs.
     */
    fun evaluate(
        formula: String,
        context: Map<String, Any?>,
        functions: Map<String, (List<Any?>) -> Any?> = FormulaFunctions.TABLE,
    ): Any? {
        if (formula.length > MAX_SOURCE_LENGTH) {
            throw FormulaError("formula too long")
        }
        val parser = Parser(Lexer(formula).tokenize())
        val node = parser.parseExpression(0)
        parser.expectEnd()
        return Evaluator(context, functions).eval(node, 0)
    }

    // ── Truthiness / numeric helpers, shared with the evaluator and the function table ──

    fun truthy(value: Any?): Boolean = when (value) {
        null -> false
        is Boolean -> value
        is Long -> value != 0L
        is Int -> value != 0
        is Double -> value != 0.0
        is Number -> value.toDouble() != 0.0
        is String -> value.isNotEmpty()
        is List<*> -> value.isNotEmpty()
        is Map<*, *> -> value.isNotEmpty()
        else -> true
    }

    /** Python treats `bool` as an `int`; arithmetic promotes it before anything else. */
    internal fun arithmetic(value: Any?): Any = when (value) {
        is Boolean -> if (value) 1L else 0L
        is Long -> value
        is Int -> value.toLong()
        is Short -> value.toLong()
        is Byte -> value.toLong()
        is Double -> value
        is Float -> value.toDouble()
        is String -> value
        else -> throw FormulaError("unsupported operand type: ${typeName(value)}")
    }

    internal fun typeName(value: Any?): String = when (value) {
        null -> "NoneType"
        is Boolean -> "bool"
        is Long, is Int -> "int"
        is Double, is Float -> "float"
        is String -> "str"
        is List<*> -> "list"
        is Map<*, *> -> "dict"
        else -> value::class.simpleName ?: "object"
    }

    internal fun isInt(value: Any?): Boolean = value is Long || value is Int || value is Boolean

    internal fun asDouble(value: Any?): Double = when (value) {
        is Boolean -> if (value) 1.0 else 0.0
        is Number -> value.toDouble()
        else -> throw FormulaError("expected a number, got ${typeName(value)}")
    }

    internal fun asLong(value: Any?): Long = when (value) {
        is Boolean -> if (value) 1L else 0L
        is Number -> value.toLong()
        else -> throw FormulaError("expected an integer, got ${typeName(value)}")
    }

    // ── Arithmetic, matching CPython's int/float promotion ──

    internal fun add(left: Any?, right: Any?): Any {
        val a = arithmetic(left)
        val b = arithmetic(right)
        if (a is String && b is String) return a + b
        if (a is String || b is String) {
            throw FormulaError("cannot add ${typeName(left)} and ${typeName(right)}")
        }
        return if (isIntLike(a) && isIntLike(b)) asLong(a) + asLong(b) else asDouble(a) + asDouble(b)
    }

    internal fun subtract(left: Any?, right: Any?): Any {
        val a = numeric(left)
        val b = numeric(right)
        return if (isIntLike(a) && isIntLike(b)) asLong(a) - asLong(b) else asDouble(a) - asDouble(b)
    }

    internal fun multiply(left: Any?, right: Any?): Any {
        val a = arithmetic(left)
        val b = arithmetic(right)
        if (a is String || b is String) {
            val text = (if (a is String) a else b) as String
            val count = asLong(if (a is String) b else a)
            if (count > MAX_STRING_MULTIPLY) throw FormulaError("string repetition too large")
            return if (count <= 0) "" else text.repeat(count.toInt())
        }
        return if (isIntLike(a) && isIntLike(b)) asLong(a) * asLong(b) else asDouble(a) * asDouble(b)
    }

    internal fun divide(left: Any?, right: Any?): Any {
        val denominator = asDouble(numeric(right))
        if (denominator == 0.0) throw FormulaError("division by zero")
        return asDouble(numeric(left)) / denominator
    }

    internal fun floorDivide(left: Any?, right: Any?): Any {
        val a = numeric(left)
        val b = numeric(right)
        if (asDouble(b) == 0.0) throw FormulaError("integer division or modulo by zero")
        if (isIntLike(a) && isIntLike(b)) return Math.floorDiv(asLong(a), asLong(b))
        return floor(asDouble(a) / asDouble(b))
    }

    internal fun modulo(left: Any?, right: Any?): Any {
        val a = numeric(left)
        val b = numeric(right)
        if (asDouble(b) == 0.0) throw FormulaError("integer division or modulo by zero")
        // Python's `%` takes the sign of the DIVISOR; Kotlin's `%` takes the sign of the dividend.
        if (isIntLike(a) && isIntLike(b)) return Math.floorMod(asLong(a), asLong(b))
        val x = asDouble(a)
        val y = asDouble(b)
        val remainder = x - floor(x / y) * y
        return remainder
    }

    internal fun power(left: Any?, right: Any?): Any {
        val a = numeric(left)
        val b = numeric(right)
        if (asDouble(b) > MAX_POW_EXPONENT) throw FormulaError("exponent too large")
        if (isIntLike(a) && isIntLike(b) && asLong(b) >= 0L) {
            var result = 1L
            var exponent = asLong(b)
            var base = asLong(a)
            while (exponent > 0) {
                if (exponent and 1L == 1L) result *= base
                base *= base
                exponent = exponent shr 1
            }
            return result
        }
        return asDouble(a).pow(asDouble(b))
    }

    internal fun negate(value: Any?): Any {
        val a = numeric(value)
        return if (isIntLike(a)) -asLong(a) else -asDouble(a)
    }

    internal fun positive(value: Any?): Any {
        val a = numeric(value)
        return if (isIntLike(a)) asLong(a) else asDouble(a)
    }

    private fun isIntLike(value: Any?): Boolean = value is Long || value is Int || value is Boolean

    private fun numeric(value: Any?): Any {
        val a = arithmetic(value)
        if (a is String) throw FormulaError("unsupported operand type: str")
        return a
    }

    /** Python `==` — numeric across int/float/bool, exact otherwise. Never coerces strings. */
    internal fun equals(left: Any?, right: Any?): Boolean {
        if (left == null || right == null) return left == null && right == null
        val leftNumeric = left is Number || left is Boolean
        val rightNumeric = right is Number || right is Boolean
        if (leftNumeric && rightNumeric) return asDouble(left) == asDouble(right)
        if (leftNumeric != rightNumeric) return false
        if (left is List<*> && right is List<*>) {
            return left.size == right.size && left.indices.all { equals(left[it], right[it]) }
        }
        return left == right
    }

    internal fun compare(left: Any?, right: Any?): Int {
        if (left is String && right is String) return left.compareTo(right)
        val leftNumeric = left is Number || left is Boolean
        val rightNumeric = right is Number || right is Boolean
        if (leftNumeric && rightNumeric) return asDouble(left).compareTo(asDouble(right))
        throw FormulaError("cannot order ${typeName(left)} and ${typeName(right)}")
    }

    // ── Lexer ──

    private enum class Kind { NUMBER, STRING, NAME, OP, END }

    private data class Token(val kind: Kind, val text: String, val literal: Any? = null)

    private class Lexer(private val source: String) {
        private var index = 0

        fun tokenize(): List<Token> {
            val tokens = mutableListOf<Token>()
            while (true) {
                skipSpace()
                if (index >= source.length) break
                val ch = source[index]
                when {
                    ch.isDigit() || (ch == '.' && index + 1 < source.length && source[index + 1].isDigit()) ->
                        tokens += number()
                    ch == '"' || ch == '\'' -> tokens += string(ch)
                    ch.isLetter() || ch == '_' -> tokens += name()
                    else -> tokens += operator()
                }
            }
            tokens += Token(Kind.END, "")
            return tokens
        }

        private fun skipSpace() {
            while (index < source.length && source[index].isWhitespace()) index++
        }

        private fun number(): Token {
            val start = index
            var isFloat = false
            while (index < source.length && (source[index].isDigit() || source[index] == '_')) index++
            if (index < source.length && source[index] == '.') {
                isFloat = true
                index++
                while (index < source.length && (source[index].isDigit() || source[index] == '_')) index++
            }
            if (index < source.length && (source[index] == 'e' || source[index] == 'E')) {
                val save = index
                index++
                if (index < source.length && (source[index] == '+' || source[index] == '-')) index++
                if (index < source.length && source[index].isDigit()) {
                    isFloat = true
                    while (index < source.length && source[index].isDigit()) index++
                } else {
                    index = save
                }
            }
            val text = source.substring(start, index).replace("_", "")
            val literal: Any = if (isFloat) {
                text.toDoubleOrNull() ?: throw FormulaError("invalid number literal: $text")
            } else {
                text.toLongOrNull() ?: text.toDoubleOrNull() ?: throw FormulaError("invalid number literal: $text")
            }
            return Token(Kind.NUMBER, text, literal)
        }

        private fun string(quote: Char): Token {
            index++
            val builder = StringBuilder()
            while (true) {
                if (index >= source.length) throw FormulaError("invalid formula syntax: unterminated string")
                val ch = source[index]
                if (ch == quote) {
                    index++
                    break
                }
                if (ch == '\\') {
                    index++
                    if (index >= source.length) throw FormulaError("invalid formula syntax: unterminated string")
                    builder.append(
                        when (source[index]) {
                            'n' -> '\n'
                            't' -> '\t'
                            'r' -> '\r'
                            '\\' -> '\\'
                            '\'' -> '\''
                            '"' -> '"'
                            else -> source[index]
                        },
                    )
                    index++
                    continue
                }
                builder.append(ch)
                index++
            }
            return Token(Kind.STRING, builder.toString(), builder.toString())
        }

        private fun name(): Token {
            val start = index
            while (index < source.length && (source[index].isLetterOrDigit() || source[index] == '_')) index++
            return Token(Kind.NAME, source.substring(start, index))
        }

        private fun operator(): Token {
            for (candidate in OPERATORS) {
                if (source.startsWith(candidate, index)) {
                    index += candidate.length
                    return Token(Kind.OP, candidate)
                }
            }
            throw FormulaError("disallowed character in formula: '${source[index]}'")
        }

        private companion object {
            // Longest-first so `**` is not read as two `*`. Note what is ABSENT: `.` (attribute
            // access), `=` (assignment / walrus), `{`, `:`, `;`, `@`, `|`, `&`, `~`, `^` — every one
            // of those is a construct the Python evaluator also refuses.
            val OPERATORS = listOf("**", "//", "==", "!=", "<=", ">=", "<", ">", "+", "-", "*", "/", "%", "(", ")", "[", "]", ",")
        }
    }

    // ── AST ──

    internal sealed class Node {
        class Literal(val value: Any?) : Node()
        class Name(val id: String) : Node()
        class Unary(val op: String, val operand: Node) : Node()
        class Binary(val op: String, val left: Node, val right: Node) : Node()
        class BoolOp(val op: String, val values: List<Node>) : Node()
        class Compare(val left: Node, val ops: List<String>, val comparators: List<Node>) : Node()
        class Conditional(val test: Node, val body: Node, val orelse: Node) : Node()
        class Call(val name: String, val args: List<Node>) : Node()
        class ListLiteral(val elements: List<Node>) : Node()
        class TupleLiteral(val elements: List<Node>) : Node()
    }

    // ── Parser (precedence exactly as CPython's grammar) ──

    private class Parser(private val tokens: List<Token>) {
        private var position = 0

        private fun peek(): Token = tokens[position]

        private fun atOp(vararg candidates: String): Boolean =
            peek().kind == Kind.OP && peek().text in candidates

        private fun atKeyword(keyword: String): Boolean =
            peek().kind == Kind.NAME && peek().text == keyword

        private fun advance(): Token = tokens[position++]

        private fun expectOp(op: String) {
            if (!atOp(op)) throw FormulaError("invalid formula syntax: expected '$op'")
            position++
        }

        fun expectEnd() {
            if (peek().kind != Kind.END) {
                throw FormulaError("invalid formula syntax: unexpected '${peek().text}'")
            }
        }

        fun parseExpression(depth: Int): Node {
            if (depth > MAX_DEPTH) throw FormulaError("formula nesting too deep")
            val body = parseOr(depth + 1)
            if (atKeyword("if")) {
                advance()
                val test = parseOr(depth + 1)
                if (!atKeyword("else")) throw FormulaError("invalid formula syntax: conditional needs 'else'")
                advance()
                return Node.Conditional(test, body, parseExpression(depth + 1))
            }
            return body
        }

        private fun parseOr(depth: Int): Node {
            if (depth > MAX_DEPTH) throw FormulaError("formula nesting too deep")
            var node = parseAnd(depth + 1)
            if (atKeyword("or")) {
                val values = mutableListOf(node)
                while (atKeyword("or")) {
                    advance()
                    values += parseAnd(depth + 1)
                }
                node = Node.BoolOp("or", values)
            }
            return node
        }

        private fun parseAnd(depth: Int): Node {
            if (depth > MAX_DEPTH) throw FormulaError("formula nesting too deep")
            var node = parseNot(depth + 1)
            if (atKeyword("and")) {
                val values = mutableListOf(node)
                while (atKeyword("and")) {
                    advance()
                    values += parseNot(depth + 1)
                }
                node = Node.BoolOp("and", values)
            }
            return node
        }

        private fun parseNot(depth: Int): Node {
            if (depth > MAX_DEPTH) throw FormulaError("formula nesting too deep")
            if (atKeyword("not")) {
                advance()
                return Node.Unary("not", parseNot(depth + 1))
            }
            return parseComparison(depth + 1)
        }

        private fun parseComparison(depth: Int): Node {
            if (depth > MAX_DEPTH) throw FormulaError("formula nesting too deep")
            val left = parseArithmetic(depth + 1)
            val ops = mutableListOf<String>()
            val comparators = mutableListOf<Node>()
            while (atOp("==", "!=", "<", "<=", ">", ">=")) {
                ops += advance().text
                comparators += parseArithmetic(depth + 1)
            }
            // `in`, `is` and `not in` are deliberately unreachable: the Python evaluator has no
            // handler for them either, and silently treating them as anything would diverge.
            if (atKeyword("in") || atKeyword("is")) {
                throw FormulaError("disallowed comparison")
            }
            return if (ops.isEmpty()) left else Node.Compare(left, ops, comparators)
        }

        private fun parseArithmetic(depth: Int): Node {
            if (depth > MAX_DEPTH) throw FormulaError("formula nesting too deep")
            var node = parseTerm(depth + 1)
            while (atOp("+", "-")) {
                val op = advance().text
                node = Node.Binary(op, node, parseTerm(depth + 1))
            }
            return node
        }

        private fun parseTerm(depth: Int): Node {
            if (depth > MAX_DEPTH) throw FormulaError("formula nesting too deep")
            var node = parseUnary(depth + 1)
            while (atOp("*", "/", "//", "%")) {
                val op = advance().text
                node = Node.Binary(op, node, parseUnary(depth + 1))
            }
            return node
        }

        private fun parseUnary(depth: Int): Node {
            if (depth > MAX_DEPTH) throw FormulaError("formula nesting too deep")
            if (atOp("+", "-")) {
                val op = advance().text
                return Node.Unary(op, parseUnary(depth + 1))
            }
            return parsePower(depth + 1)
        }

        private fun parsePower(depth: Int): Node {
            if (depth > MAX_DEPTH) throw FormulaError("formula nesting too deep")
            val base = parseAtom(depth + 1)
            if (atOp("**")) {
                advance()
                // Right-associative, and its right operand may carry a unary sign (`2 ** -1`).
                return Node.Binary("**", base, parseUnary(depth + 1))
            }
            return base
        }

        private fun parseAtom(depth: Int): Node {
            if (depth > MAX_DEPTH) throw FormulaError("formula nesting too deep")
            val token = peek()
            when (token.kind) {
                Kind.NUMBER, Kind.STRING -> {
                    advance()
                    return Node.Literal(token.literal)
                }
                Kind.NAME -> {
                    if (token.text in RESERVED) throw FormulaError("invalid formula syntax: unexpected '${token.text}'")
                    advance()
                    if (atOp("(")) {
                        advance()
                        val args = mutableListOf<Node>()
                        if (!atOp(")")) {
                            args += parseExpression(depth + 1)
                            while (atOp(",")) {
                                advance()
                                if (atOp(")")) break
                                args += parseExpression(depth + 1)
                            }
                        }
                        expectOp(")")
                        // Only a DIRECT call to a bare name. `f()()`, `obj.method()` and
                        // `table[x]()` can never be spelled, because there is no postfix
                        // call/attribute/subscript production at all.
                        return Node.Call(token.text, args)
                    }
                    return Node.Name(token.text)
                }
                Kind.OP -> {
                    if (token.text == "(") {
                        advance()
                        if (atOp(")")) {
                            advance()
                            return Node.TupleLiteral(emptyList())
                        }
                        val first = parseExpression(depth + 1)
                        if (atOp(",")) {
                            val elements = mutableListOf(first)
                            while (atOp(",")) {
                                advance()
                                if (atOp(")")) break
                                elements += parseExpression(depth + 1)
                            }
                            expectOp(")")
                            return Node.TupleLiteral(elements)
                        }
                        expectOp(")")
                        return first
                    }
                    if (token.text == "[") {
                        advance()
                        val elements = mutableListOf<Node>()
                        if (!atOp("]")) {
                            elements += parseExpression(depth + 1)
                            while (atOp(",")) {
                                advance()
                                if (atOp("]")) break
                                elements += parseExpression(depth + 1)
                            }
                        }
                        expectOp("]")
                        return Node.ListLiteral(elements)
                    }
                    throw FormulaError("invalid formula syntax: unexpected '${token.text}'")
                }
                Kind.END -> throw FormulaError("invalid formula syntax: unexpected end of formula")
            }
        }

        private companion object {
            val RESERVED = setOf("if", "else", "and", "or", "not", "in", "is", "for", "lambda", "await")
        }
    }

    // ── Evaluator ──

    private class Evaluator(
        private val context: Map<String, Any?>,
        private val functions: Map<String, (List<Any?>) -> Any?>,
    ) {
        fun eval(node: Node, depth: Int): Any? {
            if (depth > MAX_DEPTH) throw FormulaError("formula nesting too deep")
            return when (node) {
                is Node.Literal -> node.value
                is Node.Name -> {
                    if (!context.containsKey(node.id)) throw FormulaError("unknown name: ${node.id}")
                    context[node.id]
                }
                is Node.Unary -> when (node.op) {
                    "not" -> !truthy(eval(node.operand, depth + 1))
                    "-" -> negate(eval(node.operand, depth + 1))
                    else -> positive(eval(node.operand, depth + 1))
                }
                is Node.Binary -> {
                    val left = eval(node.left, depth + 1)
                    val right = eval(node.right, depth + 1)
                    when (node.op) {
                        "+" -> add(left, right)
                        "-" -> subtract(left, right)
                        "*" -> multiply(left, right)
                        "/" -> divide(left, right)
                        "//" -> floorDivide(left, right)
                        "%" -> modulo(left, right)
                        "**" -> power(left, right)
                        else -> throw FormulaError("disallowed operator: ${node.op}")
                    }
                }
                is Node.BoolOp -> {
                    // The Python evaluator materialises every operand before selecting, so an
                    // unknown name on the right of a short-circuit still raises. Match that.
                    val values = node.values.map { eval(it, depth + 1) }
                    if (node.op == "and") {
                        values.firstOrNull { !truthy(it) } ?: values.last()
                    } else {
                        values.firstOrNull { truthy(it) } ?: values.last()
                    }
                }
                is Node.Compare -> {
                    var left = eval(node.left, depth + 1)
                    for (i in node.ops.indices) {
                        val right = eval(node.comparators[i], depth + 1)
                        val passed = when (node.ops[i]) {
                            "==" -> SafeFormula.equals(left, right)
                            "!=" -> !SafeFormula.equals(left, right)
                            "<" -> compare(left, right) < 0
                            "<=" -> compare(left, right) <= 0
                            ">" -> compare(left, right) > 0
                            ">=" -> compare(left, right) >= 0
                            else -> throw FormulaError("disallowed comparison")
                        }
                        if (!passed) return false
                        left = right
                    }
                    true
                }
                is Node.Conditional ->
                    if (truthy(eval(node.test, depth + 1))) eval(node.body, depth + 1) else eval(node.orelse, depth + 1)
                is Node.Call -> {
                    val function = functions[node.name] ?: throw FormulaError("unknown function: ${node.name}")
                    val args = node.args.map { eval(it, depth + 1) }
                    try {
                        function(args)
                    } catch (error: FormulaError) {
                        throw error
                    } catch (error: Exception) {
                        throw FormulaError("${node.name} failed: ${error.message}")
                    }
                }
                is Node.ListLiteral -> node.elements.map { eval(it, depth + 1) }
                is Node.TupleLiteral -> node.elements.map { eval(it, depth + 1) }
            }
        }
    }
}

/**
 * The formula function table. This is the Kotlin subset of the Python core's
 * `EXCEL_FUNCTIONS + _FORMULA_BUILTINS`: the numeric, statistical and logical functions a
 * scorecard or metric formula realistically calls, ported with the Python implementations'
 * exact return types (`ABS` is a float, `SIGN` is an int, `INT` floors, `TRUNC` truncates).
 *
 * The rest of the ~230-entry Excel table is NOT ported. A formula that calls one of those
 * raises `unknown function: NAME` — loud and identical in shape to Python's own message for a
 * name it does not know — rather than silently producing a different score.
 */
object FormulaFunctions {
    /** `_num` in excel_functions.py: coerce to float, falling back to [fallback]. */
    private fun num(value: Any?, fallback: Double = 0.0): Double = when (value) {
        is Boolean -> if (value) 1.0 else 0.0
        is Number -> value.toDouble()
        is String -> value.trim().toDoubleOrNull() ?: fallback
        else -> fallback
    }

    /** `_flat`: flatten nested lists, preserving element types. */
    private fun flat(args: List<Any?>): List<Any?> {
        val out = mutableListOf<Any?>()
        for (arg in args) {
            if (arg is List<*>) {
                out.addAll(flat(arg))
            } else {
                out.add(arg)
            }
        }
        return out
    }

    /** `_nums`: flatten, coerce to float, DROP anything non-numeric (including unparsable strings). */
    private fun nums(args: List<Any?>): List<Double> {
        val out = mutableListOf<Double>()
        for (arg in args) {
            when (arg) {
                is List<*> -> out += nums(arg)
                is Boolean -> out += if (arg) 1.0 else 0.0
                is Number -> out += arg.toDouble()
                is String -> arg.trim().toDoubleOrNull()?.let { out += it }
                else -> Unit
            }
        }
        return out
    }

    private fun arg(args: List<Any?>, index: Int, fallback: Any? = null): Any? =
        if (index < args.size) args[index] else fallback

    private fun require(args: List<Any?>, count: Int, name: String) {
        if (args.size < count) throw FormulaError("$name expects at least $count argument(s)")
    }

    /**
     * Python's `round()` — round-half-to-EVEN, not half-away-from-zero. The WoE score runs
     * through this, so getting it wrong shifts a credit score by a point at every .5 boundary.
     */
    fun bankersRound(value: Double): Long {
        if (value.isNaN() || value.isInfinite()) throw FormulaError("cannot round $value")
        return Math.rint(value).toLong()
    }

    private fun bankersRound(value: Double, digits: Int): Double {
        if (value.isNaN() || value.isInfinite()) return value
        val scale = 10.0.pow(digits.toDouble())
        if (scale.isInfinite() || scale == 0.0) return value
        return Math.rint(value * scale) / scale
    }

    private fun pyTruncate(value: Double): Long {
        if (value.isNaN() || value.isInfinite()) throw FormulaError("cannot convert $value to an integer")
        return truncate(value).toLong()
    }

    val TABLE: Map<String, (List<Any?>) -> Any?> = buildMap {
        // ── Math ──
        put("ABS") { args -> require(args, 1, "ABS"); abs(num(args[0])) }
        put("SQRT") { args -> require(args, 1, "SQRT"); sqrt(num(args[0])) }
        put("POWER") { args -> require(args, 2, "POWER"); num(args[0]).pow(num(args[1])) }
        put("EXP") { args -> require(args, 1, "EXP"); exp(num(args[0])) }
        put("LN") { args ->
            require(args, 1, "LN")
            val x = num(args[0])
            if (x <= 0.0) throw FormulaError("math domain error")
            ln(x)
        }
        put("LOG") { args ->
            require(args, 1, "LOG")
            val x = num(args[0])
            val base = num(arg(args, 1, 10.0), 10.0)
            if (x <= 0.0 || base <= 0.0 || base == 1.0) throw FormulaError("math domain error")
            ln(x) / ln(base)
        }
        put("LOG10") { args ->
            require(args, 1, "LOG10")
            val x = num(args[0])
            if (x <= 0.0) throw FormulaError("math domain error")
            log10(x)
        }
        put("MOD") { args ->
            require(args, 2, "MOD")
            val divisor = num(args[1])
            if (divisor == 0.0) throw FormulaError("MOD: divisor cannot be zero")
            val n = num(args[0])
            n - floor(n / divisor) * divisor
        }
        put("SIGN") { args ->
            require(args, 1, "SIGN")
            val n = num(args[0])
            if (n > 0) 1L else if (n < 0) -1L else 0L
        }
        put("INT") { args -> require(args, 1, "INT"); floor(num(args[0])).toLong() }
        put("TRUNC") { args ->
            require(args, 1, "TRUNC")
            val scale = 10.0.pow(SafeFormula.asLong(arg(args, 1, 0L) ?: 0L).toDouble())
            pyTruncate(num(args[0]) * scale) / scale
        }
        put("ROUND") { args ->
            require(args, 1, "ROUND")
            bankersRound(num(args[0]), SafeFormula.asLong(arg(args, 1, 0L) ?: 0L).toInt())
        }
        put("ROUNDUP") { args ->
            require(args, 1, "ROUNDUP")
            val scale = 10.0.pow(SafeFormula.asLong(arg(args, 1, 0L) ?: 0L).toDouble())
            val n = num(args[0])
            if (n >= 0) ceil(n * scale) / scale else floor(n * scale) / scale
        }
        put("ROUNDDOWN") { args ->
            require(args, 1, "ROUNDDOWN")
            val scale = 10.0.pow(SafeFormula.asLong(arg(args, 1, 0L) ?: 0L).toDouble())
            truncate(num(args[0]) * scale) / scale
        }
        put("CEILING") { args ->
            require(args, 1, "CEILING")
            val significance = num(arg(args, 1, 1.0), 1.0)
            if (significance == 0.0) 0.0 else ceil(num(args[0]) / significance) * significance
        }
        put("FLOOR") { args ->
            require(args, 1, "FLOOR")
            val significance = num(arg(args, 1, 1.0), 1.0)
            if (significance == 0.0) 0.0 else floor(num(args[0]) / significance) * significance
        }
        put("PI") { _ -> Math.PI }
        put("SUM") { args -> val values = nums(args); if (values.isEmpty()) 0L else values.sum() }
        put("PRODUCT") { args -> nums(args).fold(1.0) { acc, value -> acc * value } }

        // ── Statistical ──
        put("MIN") { args -> nums(args).minOrNull() ?: 0.0 }
        put("MAX") { args -> nums(args).maxOrNull() ?: 0.0 }
        put("AVERAGE") { args ->
            val values = nums(args)
            if (values.isEmpty()) throw FormulaError("AVERAGE: no numeric values")
            values.sum() / values.size
        }
        put("MEDIAN") { args ->
            val values = nums(args).sorted()
            if (values.isEmpty()) throw FormulaError("MEDIAN: no values")
            val middle = values.size / 2
            if (values.size % 2 == 1) values[middle] else (values[middle - 1] + values[middle]) / 2.0
        }
        put("COUNT") { args -> nums(args).size.toLong() }

        // ── Logical ──
        put("AND") { args -> flat(args).all { SafeFormula.truthy(it) } }
        put("OR") { args -> flat(args).any { SafeFormula.truthy(it) } }
        put("NOT") { args -> require(args, 1, "NOT"); !SafeFormula.truthy(args[0]) }
        put("XOR") { args -> flat(args).count { SafeFormula.truthy(it) } % 2 == 1 }
        put("IF") { args ->
            require(args, 1, "IF")
            if (SafeFormula.truthy(args[0])) arg(args, 1, true) else arg(args, 2, false)
        }
        put("IFS") { args ->
            var i = 0
            var answer: Any? = null
            var found = false
            while (i < args.size) {
                if (SafeFormula.truthy(args[i]) && i + 1 < args.size) {
                    answer = args[i + 1]
                    found = true
                    break
                }
                i += 2
            }
            if (!found) throw FormulaError("IFS: no condition is TRUE")
            answer
        }
        put("TRUE") { _ -> true }
        put("FALSE") { _ -> false }

        // ── Plain builtins the Python table also exposes (lowercase, so no collision above) ──
        put("abs") { args ->
            require(args, 1, "abs")
            when (val value = SafeFormula.arithmetic(args[0])) {
                is Long -> abs(value)
                is Double -> abs(value)
                else -> throw FormulaError("abs failed: bad operand type")
            }
        }
        put("min") { args -> extremum(args, wantMax = false) }
        put("max") { args -> extremum(args, wantMax = true) }
        put("round") { args ->
            require(args, 1, "round")
            if (args.size >= 2) bankersRound(SafeFormula.asDouble(args[0]), SafeFormula.asLong(args[1]).toInt())
            else bankersRound(SafeFormula.asDouble(args[0]))
        }
        put("int") { args ->
            require(args, 1, "int")
            when (val value = args[0]) {
                is Boolean -> if (value) 1L else 0L
                is Number -> pyTruncate(value.toDouble())
                is String -> value.trim().toLongOrNull()
                    ?: throw FormulaError("int failed: invalid literal for int(): '$value'")
                else -> throw FormulaError("int failed: bad operand type")
            }
        }
        put("float") { args ->
            require(args, 1, "float")
            when (val value = args[0]) {
                is Boolean -> if (value) 1.0 else 0.0
                is Number -> value.toDouble()
                is String -> value.trim().toDoubleOrNull()
                    ?: throw FormulaError("float failed: could not convert string to float: '$value'")
                else -> throw FormulaError("float failed: bad operand type")
            }
        }
        put("str") { args -> require(args, 1, "str"); pyStr(args[0]) }
        put("bool") { args -> require(args, 1, "bool"); SafeFormula.truthy(args[0]) }
        put("len") { args ->
            require(args, 1, "len")
            when (val value = args[0]) {
                is String -> value.length.toLong()
                is List<*> -> value.size.toLong()
                is Map<*, *> -> value.size.toLong()
                else -> throw FormulaError("len failed: object of type ${SafeFormula.typeName(value)} has no len()")
            }
        }
        put("sum") { args ->
            require(args, 1, "sum")
            val items = when (val value = args[0]) {
                is List<*> -> value
                else -> throw FormulaError("sum failed: ${SafeFormula.typeName(value)} is not iterable")
            }
            var total: Any = if (args.size >= 2) SafeFormula.arithmetic(args[1]) else 0L
            for (item in items) total = SafeFormula.add(total, item)
            total
        }
    }

    private fun extremum(args: List<Any?>, wantMax: Boolean): Any? {
        val items: List<Any?> = if (args.size == 1 && args[0] is List<*>) (args[0] as List<*>) else args
        if (items.isEmpty()) throw FormulaError("${if (wantMax) "max" else "min"} failed: empty sequence")
        var best = items[0]
        for (item in items.drop(1)) {
            val ordering = SafeFormula.compare(item, best)
            if (if (wantMax) ordering > 0 else ordering < 0) best = item
        }
        return best
    }

    /** Python's `str()` for the value kinds a formula can produce. */
    private fun pyStr(value: Any?): String = when (value) {
        null -> "None"
        is Boolean -> if (value) "True" else "False"
        is Double ->
            if (value.isNaN()) "nan"
            else if (value.isInfinite()) if (value > 0) "inf" else "-inf"
            else if (value == floor(value) && abs(value) < 1e16) "${value.toLong()}.0"
            else value.toString()
        is String -> value
        else -> value.toString()
    }
}
