"""
Comprehensive tests for the Excel-compatible function library.

Tests 60+ functions across all categories: Math & Trig, Statistical,
Financial, Logical, Text, Lookup & Reference, Date & Time, Information,
Engineering, Database, and edge cases.
"""

import os
import sys
import math
import unittest
from datetime import date, datetime
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from app.excel_functions import (
    EXCEL_FUNCTIONS,
    # Math & Trig
    ABS, CEILING, FLOOR_FUNC, MOD, POWER, SQRT, ROUND_FUNC, SUM_FUNC,
    SUMPRODUCT, PI, FACT, GCD, LCM, AVERAGE, PRODUCT, SUMSQ, EXP, LN,
    # Statistical
    MEDIAN, STDEV, PERCENTILE, COUNT, COUNTIF, LARGE, SMALL, RANK, CORREL,
    STDEVP, MODE, COUNTBLANK, COUNTA,
    # Financial
    PMT, FV, PV, NPV, IRR, NPER, RATE, EFFECT, NOMINAL, SLN,
    # Logical
    IF_FUNC, AND_FUNC, OR_FUNC, NOT_FUNC, IFS, SWITCH, IFERROR, XOR,
    # Text
    CONCATENATE, LEFT, RIGHT, MID, LEN_FUNC, UPPER, LOWER, TRIM, FIND,
    SUBSTITUTE, TEXTJOIN, PROPER, REPLACE, REPT,
    # Lookup
    VLOOKUP, HLOOKUP, INDEX, MATCH, XLOOKUP, CHOOSE, ROWS, COLUMNS,
    # Date
    DATE, YEAR, MONTH, DAY, DAYS, EDATE, EOMONTH, NETWORKDAYS, DATEDIF,
    # Information
    ISBLANK, ISNUMBER, ISTEXT, ISERROR, ISNA, TYPE_FUNC, N,
    # Engineering
    BIN2DEC, DEC2BIN, HEX2DEC, CONVERT, DEC2HEX, HEX2BIN,
    # Database
    DSUM, DAVERAGE, DCOUNT,
    # Helpers
    _match_criteria, _num, _nums,
    # Additional
    SUMIF, COUNTIFS, MAX_FUNC, MIN_FUNC, SIGN, TRUNC, ROUNDUP, ROUNDDOWN,
)


class ExcelMathTests(unittest.TestCase):
    """Tests for Math & Trig functions."""

    def setUp(self):
        """Set up test fixtures for math tests."""
        self.simple_list = [1, 2, 3, 4, 5]
        self.float_list = [1.5, 2.5, 3.5]

    def test_sum(self):
        """SUM should add all numeric values."""
        self.assertEqual(SUM_FUNC(1, 2, 3), 6.0)
        self.assertEqual(SUM_FUNC(*self.simple_list), 15.0)
        self.assertEqual(SUM_FUNC([1, 2], [3, 4]), 10.0)

    def test_sum_via_registry(self):
        """SUM should be accessible via EXCEL_FUNCTIONS dict."""
        fn = EXCEL_FUNCTIONS["SUM"]
        self.assertEqual(fn(10, 20, 30), 60.0)

    def test_average(self):
        """AVERAGE should return arithmetic mean."""
        self.assertEqual(AVERAGE(2, 4, 6), 4.0)
        self.assertAlmostEqual(AVERAGE(1, 2, 3, 4, 5), 3.0)

    def test_average_no_values(self):
        """AVERAGE should raise on empty input."""
        with self.assertRaises(ValueError):
            AVERAGE()

    def test_round(self):
        """ROUND should round to specified digits."""
        self.assertEqual(ROUND_FUNC(2.567, 2), 2.57)
        self.assertEqual(ROUND_FUNC(3.5, 0), 4.0)
        self.assertEqual(ROUND_FUNC(-1.5, 0), -2.0)

    def test_ceiling(self):
        """CEILING should round up to nearest significance."""
        self.assertEqual(CEILING(2.5, 1), 3.0)
        self.assertEqual(CEILING(4.42, 0.05), 4.45)
        self.assertEqual(CEILING(0, 5), 0.0)

    def test_floor(self):
        """FLOOR should round down to nearest significance."""
        self.assertEqual(FLOOR_FUNC(3.7, 1), 3.0)
        self.assertEqual(FLOOR_FUNC(2.5, 1), 2.0)
        self.assertEqual(FLOOR_FUNC(-2.5, 2), -4.0)

    def test_mod(self):
        """MOD should return remainder."""
        self.assertEqual(MOD(10, 3), 1.0)
        self.assertEqual(MOD(7, 2), 1.0)
        self.assertEqual(MOD(10, 5), 0.0)

    def test_mod_zero_divisor(self):
        """MOD should raise on zero divisor."""
        with self.assertRaises(ValueError):
            MOD(10, 0)

    def test_power(self):
        """POWER should compute base^exponent."""
        self.assertEqual(POWER(2, 3), 8.0)
        self.assertEqual(POWER(5, 0), 1.0)
        self.assertAlmostEqual(POWER(9, 0.5), 3.0)

    def test_sqrt(self):
        """SQRT should return square root."""
        self.assertEqual(SQRT(16), 4.0)
        self.assertEqual(SQRT(0), 0.0)
        self.assertAlmostEqual(SQRT(2), 1.4142135623730951)

    def test_abs(self):
        """ABS should return absolute value."""
        self.assertEqual(ABS(-5), 5.0)
        self.assertEqual(ABS(5), 5.0)
        self.assertEqual(ABS(0), 0.0)

    def test_sumproduct(self):
        """SUMPRODUCT should sum element-wise products."""
        self.assertEqual(SUMPRODUCT([1, 2, 3], [4, 5, 6]), 32.0)
        # 1*4 + 2*5 + 3*6 = 4 + 10 + 18 = 32
        self.assertEqual(SUMPRODUCT([2, 3], [10, 20]), 80.0)

    def test_pi(self):
        """PI should return math.pi."""
        self.assertAlmostEqual(PI(), 3.141592653589793)

    def test_fact(self):
        """FACT should return factorial."""
        self.assertEqual(FACT(5), 120)
        self.assertEqual(FACT(0), 1)
        self.assertEqual(FACT(1), 1)

    def test_gcd(self):
        """GCD should return greatest common divisor."""
        self.assertEqual(GCD(12, 8), 4)
        self.assertEqual(GCD(24, 36, 48), 12)

    def test_lcm(self):
        """LCM should return least common multiple."""
        self.assertEqual(LCM(4, 6), 12)
        self.assertEqual(LCM(3, 5, 10), 30)

    def test_product(self):
        """PRODUCT should multiply all values."""
        self.assertEqual(PRODUCT(2, 3, 4), 24.0)
        self.assertEqual(PRODUCT(5, 0), 0.0)

    def test_sumsq(self):
        """SUMSQ should sum squares."""
        self.assertEqual(SUMSQ(1, 2, 3), 14.0)
        # 1 + 4 + 9 = 14

    def test_sign(self):
        """SIGN should return -1, 0, or 1."""
        self.assertEqual(SIGN(10), 1)
        self.assertEqual(SIGN(-10), -1)
        self.assertEqual(SIGN(0), 0)

    def test_trunc(self):
        """TRUNC should truncate toward zero."""
        self.assertEqual(TRUNC(8.9), 8.0)
        self.assertEqual(TRUNC(-8.9), -8.0)
        self.assertEqual(TRUNC(3.14159, 2), 3.14)

    def test_roundup(self):
        """ROUNDUP should round away from zero."""
        self.assertEqual(ROUNDUP(3.2, 0), 4.0)
        self.assertEqual(ROUNDUP(-3.2, 0), -4.0)

    def test_rounddown(self):
        """ROUNDDOWN should round toward zero."""
        self.assertEqual(ROUNDDOWN(3.9, 0), 3.0)
        self.assertEqual(ROUNDDOWN(-3.9, 0), -3.0)

    def test_exp_ln(self):
        """EXP and LN should be inverses."""
        self.assertAlmostEqual(LN(EXP(1)), 1.0)
        self.assertAlmostEqual(EXP(LN(5)), 5.0)


class ExcelStatTests(unittest.TestCase):
    """Tests for Statistical functions."""

    def setUp(self):
        """Set up test fixtures for statistical tests."""
        self.data = [4, 8, 15, 16, 23, 42]
        self.data2 = [1, 2, 3, 4, 5]

    def test_median_odd(self):
        """MEDIAN of odd-count list should return middle value."""
        self.assertEqual(MEDIAN(1, 3, 5), 3.0)

    def test_median_even(self):
        """MEDIAN of even-count list should return average of two middle values."""
        self.assertEqual(MEDIAN(1, 2, 3, 4), 2.5)

    def test_stdev(self):
        """STDEV should return sample standard deviation."""
        result = STDEV(2, 4, 4, 4, 5, 5, 7, 9)
        self.assertAlmostEqual(result, 2.13809, places=3)

    def test_stdev_min_values(self):
        """STDEV should raise with fewer than 2 values."""
        with self.assertRaises(ValueError):
            STDEV(5)

    def test_stdevp(self):
        """STDEVP should return population standard deviation."""
        result = STDEVP(2, 4, 4, 4, 5, 5, 7, 9)
        self.assertAlmostEqual(result, 2.0, places=0)

    def test_percentile(self):
        """PERCENTILE should interpolate correctly."""
        self.assertEqual(PERCENTILE([1, 2, 3, 4], 0.0), 1.0)
        self.assertEqual(PERCENTILE([1, 2, 3, 4], 1.0), 4.0)
        self.assertAlmostEqual(PERCENTILE([1, 2, 3, 4], 0.5), 2.5)

    def test_percentile_invalid_k(self):
        """PERCENTILE should raise if k is out of [0,1]."""
        with self.assertRaises(ValueError):
            PERCENTILE([1, 2, 3], 1.5)

    def test_count(self):
        """COUNT should count numeric values only."""
        self.assertEqual(COUNT(1, 2, "hello", 3, None), 3)
        self.assertEqual(COUNT(*self.data), 6)

    def test_counta(self):
        """COUNTA should count non-blank values."""
        self.assertEqual(COUNTA(1, "hello", None, "", 0), 3)

    def test_countblank(self):
        """COUNTBLANK should count blanks."""
        self.assertEqual(COUNTBLANK(None, "", 0, "text"), 2)

    def test_countif(self):
        """COUNTIF should count values matching criteria."""
        self.assertEqual(COUNTIF([1, 2, 3, 4, 5], ">3"), 2)
        self.assertEqual(COUNTIF([10, 20, 30, 40], ">=20"), 3)
        self.assertEqual(COUNTIF(["apple", "banana", "apple"], "apple"), 2)

    def test_large(self):
        """LARGE should return the k-th largest."""
        self.assertEqual(LARGE([3, 5, 2, 8, 1], 1), 8.0)
        self.assertEqual(LARGE([3, 5, 2, 8, 1], 2), 5.0)
        self.assertEqual(LARGE([3, 5, 2, 8, 1], 5), 1.0)

    def test_small(self):
        """SMALL should return the k-th smallest."""
        self.assertEqual(SMALL([3, 5, 2, 8, 1], 1), 1.0)
        self.assertEqual(SMALL([3, 5, 2, 8, 1], 3), 3.0)

    def test_rank(self):
        """RANK should return position in descending order by default."""
        data = [7, 3, 9, 1, 5]
        self.assertEqual(RANK(9, data, 0), 1)
        self.assertEqual(RANK(1, data, 0), 5)
        # Ascending
        self.assertEqual(RANK(1, data, 1), 1)

    @unittest.skipUnless(
        hasattr(__import__("statistics"), "correlation"),
        "statistics.correlation requires Python 3.10+",
    )
    def test_correl(self):
        """CORREL should return Pearson correlation coefficient."""
        x = [1, 2, 3, 4, 5]
        y = [2, 4, 6, 8, 10]
        self.assertAlmostEqual(CORREL(x, y), 1.0, places=5)

    @unittest.skipUnless(
        hasattr(__import__("statistics"), "correlation"),
        "statistics.correlation requires Python 3.10+",
    )
    def test_correl_negative(self):
        """CORREL should handle negative correlation."""
        x = [1, 2, 3, 4, 5]
        y = [10, 8, 6, 4, 2]
        self.assertAlmostEqual(CORREL(x, y), -1.0, places=5)

    def test_mode(self):
        """MODE should return most frequent value."""
        self.assertEqual(MODE(1, 2, 2, 3, 3, 3), 3.0)

    def test_max_min(self):
        """MAX and MIN should find extremes."""
        self.assertEqual(MAX_FUNC(1, 5, 3, 9, 2), 9.0)
        self.assertEqual(MIN_FUNC(1, 5, 3, 9, 2), 1.0)
        # Empty returns 0
        self.assertEqual(MAX_FUNC(), 0.0)
        self.assertEqual(MIN_FUNC(), 0.0)


class ExcelFinancialTests(unittest.TestCase):
    """Tests for Financial functions."""

    def test_pmt(self):
        """PMT should compute periodic payment for a loan."""
        # $200,000 loan, 5% annual rate, 30 years monthly
        result = PMT(0.05 / 12, 360, -200000)
        self.assertAlmostEqual(result, 1073.64, places=2)

    def test_pmt_zero_rate(self):
        """PMT with zero rate should be simple division."""
        result = PMT(0, 12, -1200)
        self.assertAlmostEqual(result, 100.0, places=2)

    def test_fv(self):
        """FV should compute future value of an investment."""
        # $100/month at 6% annual for 10 years
        result = FV(0.06 / 12, 120, -100)
        self.assertAlmostEqual(result, 16387.93, places=2)

    def test_pv(self):
        """PV should compute present value."""
        # How much to invest now for $100/month for 10 years at 6%
        result = PV(0.06 / 12, 120, -100)
        self.assertAlmostEqual(result, 9007.35, places=0)

    def test_npv(self):
        """NPV should discount cash flows at given rate."""
        result = NPV(0.1, -10000, 3000, 4200, 6800)
        self.assertAlmostEqual(result, 1188.44, places=0)

    def test_irr(self):
        """IRR should find the rate that makes NPV zero."""
        result = IRR([-100, 30, 30, 30, 30])
        self.assertAlmostEqual(result, 0.0771, places=2)

    def test_nper(self):
        """NPER should compute number of periods."""
        # How many months to pay off $1000 at 1%/month with $100 payments
        result = NPER(0.01, -100, 1000)
        self.assertAlmostEqual(result, 10.58, places=1)

    def test_rate(self):
        """RATE should find the periodic interest rate."""
        # 48 payments of $500 on $20000 loan
        result = RATE(48, -500, 20000)
        self.assertAlmostEqual(result, 0.00770, places=3)

    def test_effect(self):
        """EFFECT should convert nominal to effective rate."""
        result = EFFECT(0.10, 4)
        self.assertAlmostEqual(result, 0.10381, places=4)

    def test_nominal(self):
        """NOMINAL should convert effective to nominal rate."""
        result = NOMINAL(0.10381289, 4)
        self.assertAlmostEqual(result, 0.10, places=2)

    def test_sln(self):
        """SLN should compute straight-line depreciation."""
        result = SLN(30000, 7500, 10)
        self.assertEqual(result, 2250.0)


class ExcelLogicalTests(unittest.TestCase):
    """Tests for Logical functions."""

    def test_if_true(self):
        """IF should return true-branch when condition is truthy."""
        self.assertEqual(IF_FUNC(True, "yes", "no"), "yes")
        self.assertEqual(IF_FUNC(1, "yes", "no"), "yes")

    def test_if_false(self):
        """IF should return false-branch when condition is falsy."""
        self.assertEqual(IF_FUNC(False, "yes", "no"), "no")
        self.assertEqual(IF_FUNC(0, "yes", "no"), "no")

    def test_and(self):
        """AND should return True only when all args are truthy."""
        self.assertTrue(AND_FUNC(True, True, 1))
        self.assertFalse(AND_FUNC(True, False, 1))

    def test_or(self):
        """OR should return True when any arg is truthy."""
        self.assertTrue(OR_FUNC(False, True, 0))
        self.assertFalse(OR_FUNC(False, 0, ""))

    def test_not(self):
        """NOT should invert boolean."""
        self.assertTrue(NOT_FUNC(False))
        self.assertFalse(NOT_FUNC(True))
        self.assertTrue(NOT_FUNC(0))

    def test_ifs(self):
        """IFS should return value for first true condition."""
        self.assertEqual(IFS(False, "a", True, "b", True, "c"), "b")

    def test_ifs_no_match(self):
        """IFS should raise when no condition is true."""
        with self.assertRaises(ValueError):
            IFS(False, "a", False, "b")

    def test_switch(self):
        """SWITCH should return matching value."""
        self.assertEqual(SWITCH(2, 1, "one", 2, "two", 3, "three"), "two")

    def test_switch_default(self):
        """SWITCH should return default when no match."""
        self.assertEqual(SWITCH(99, 1, "one", 2, "two", "other"), "other")

    def test_iferror(self):
        """IFERROR should return fallback for NaN/Inf."""
        self.assertEqual(IFERROR(float("nan"), "error"), "error")
        self.assertEqual(IFERROR(float("inf"), "error"), "error")
        self.assertEqual(IFERROR(42, "error"), 42)

    def test_xor(self):
        """XOR should return True when odd number of args are truthy."""
        self.assertTrue(XOR(True, False))
        self.assertFalse(XOR(True, True))
        self.assertTrue(XOR(True, False, False))
        self.assertFalse(XOR(True, True, True, True))


class ExcelTextTests(unittest.TestCase):
    """Tests for Text functions."""

    def test_concatenate(self):
        """CONCATENATE should join strings."""
        self.assertEqual(CONCATENATE("Hello", " ", "World"), "Hello World")
        self.assertEqual(CONCATENATE("A", "B", "C"), "ABC")

    def test_left(self):
        """LEFT should extract leftmost characters."""
        self.assertEqual(LEFT("Hello World", 5), "Hello")
        self.assertEqual(LEFT("ABC", 1), "A")

    def test_right(self):
        """RIGHT should extract rightmost characters."""
        self.assertEqual(RIGHT("Hello World", 5), "World")
        self.assertEqual(RIGHT("ABC", 1), "C")

    def test_mid(self):
        """MID should extract substring from middle."""
        self.assertEqual(MID("Hello World", 7, 5), "World")
        self.assertEqual(MID("ABCDEF", 2, 3), "BCD")

    def test_len(self):
        """LEN should return string length."""
        self.assertEqual(LEN_FUNC("Hello"), 5)
        self.assertEqual(LEN_FUNC(""), 0)

    def test_upper(self):
        """UPPER should convert to uppercase."""
        self.assertEqual(UPPER("hello"), "HELLO")
        self.assertEqual(UPPER("Hello World"), "HELLO WORLD")

    def test_lower(self):
        """LOWER should convert to lowercase."""
        self.assertEqual(LOWER("HELLO"), "hello")

    def test_trim(self):
        """TRIM should remove extra whitespace."""
        self.assertEqual(TRIM("  hello   world  "), "hello world")
        self.assertEqual(TRIM("no  extra  spaces"), "no extra spaces")

    def test_find(self):
        """FIND should return 1-based position (case sensitive)."""
        self.assertEqual(FIND("World", "Hello World"), 7)
        self.assertEqual(FIND("l", "Hello"), 3)

    def test_find_not_found(self):
        """FIND should raise when text is not found."""
        with self.assertRaises(ValueError):
            FIND("xyz", "Hello World")

    def test_substitute(self):
        """SUBSTITUTE should replace text occurrences."""
        self.assertEqual(SUBSTITUTE("Hello World", "World", "Python"), "Hello Python")
        # Replace specific instance
        self.assertEqual(SUBSTITUTE("a-b-c-d", "-", "/", 2), "a-b/c-d")

    def test_textjoin(self):
        """TEXTJOIN should join with delimiter."""
        self.assertEqual(TEXTJOIN(", ", False, "a", "b", "c"), "a, b, c")
        self.assertEqual(TEXTJOIN("-", True, "a", "", "c"), "a-c")

    def test_proper(self):
        """PROPER should title-case text."""
        self.assertEqual(PROPER("hello world"), "Hello World")

    def test_replace(self):
        """REPLACE should replace by position."""
        self.assertEqual(REPLACE("Hello", 1, 5, "World"), "World")
        self.assertEqual(REPLACE("abcdef", 3, 2, "XY"), "abXYef")

    def test_rept(self):
        """REPT should repeat text n times."""
        self.assertEqual(REPT("ab", 3), "ababab")
        self.assertEqual(REPT("x", 0), "")


class ExcelLookupTests(unittest.TestCase):
    """Tests for Lookup & Reference functions."""

    def setUp(self):
        """Set up test fixtures for lookup tests."""
        self.table = [
            ["ID", "Name", "Score"],
            [1, "Alice", 85],
            [2, "Bob", 92],
            [3, "Carol", 78],
            [4, "Dave", 95],
        ]

    def test_vlookup_exact(self):
        """VLOOKUP should find exact match."""
        data = self.table[1:]  # exclude header
        self.assertEqual(VLOOKUP(2, data, 2, False), "Bob")
        self.assertEqual(VLOOKUP(3, data, 3, False), 78)

    def test_vlookup_not_found(self):
        """VLOOKUP should raise when no exact match."""
        data = self.table[1:]
        with self.assertRaises(ValueError):
            VLOOKUP(99, data, 2, False)

    def test_hlookup_exact(self):
        """HLOOKUP should find exact match in columns."""
        htable = [
            [1, 2, 3, 4],
            ["A", "B", "C", "D"],
            [10, 20, 30, 40],
        ]
        self.assertEqual(HLOOKUP(3, htable, 2, False), "C")
        self.assertEqual(HLOOKUP(2, htable, 3, False), 20)

    def test_index(self):
        """INDEX should return element by row and column."""
        array = [[1, 2, 3], [4, 5, 6], [7, 8, 9]]
        self.assertEqual(INDEX(array, 2, 3), 6)
        self.assertEqual(INDEX(array, 1, 1), 1)

    def test_index_row_only(self):
        """INDEX with only row should return entire row."""
        array = [[10, 20], [30, 40]]
        self.assertEqual(INDEX(array, 1), [10, 20])

    def test_match_exact(self):
        """MATCH with match_type=0 should find exact position."""
        self.assertEqual(MATCH("Carol", ["Alice", "Bob", "Carol", "Dave"], 0), 3)
        self.assertEqual(MATCH(30, [10, 20, 30, 40], 0), 3)

    def test_xlookup(self):
        """XLOOKUP should find and return from parallel arrays."""
        ids = [1, 2, 3, 4]
        names = ["Alice", "Bob", "Carol", "Dave"]
        self.assertEqual(XLOOKUP(3, ids, names), "Carol")

    def test_xlookup_not_found_default(self):
        """XLOOKUP should return if_not_found when no match."""
        self.assertEqual(XLOOKUP(99, [1, 2, 3], ["a", "b", "c"], "N/A"), "N/A")

    def test_choose(self):
        """CHOOSE should return value at specified index."""
        self.assertEqual(CHOOSE(2, "Sun", "Mon", "Tue"), "Mon")

    def test_rows_columns(self):
        """ROWS and COLUMNS should return dimensions."""
        arr = [[1, 2, 3], [4, 5, 6]]
        self.assertEqual(ROWS(arr), 2)
        self.assertEqual(COLUMNS(arr), 3)


class ExcelDateTests(unittest.TestCase):
    """Tests for Date & Time functions."""

    def setUp(self):
        """Set up test fixtures for date tests."""
        self.d1 = date(2024, 1, 15)
        self.d2 = date(2024, 6, 30)

    def test_date(self):
        """DATE should create a date object."""
        d = DATE(2024, 3, 15)
        self.assertEqual(d, date(2024, 3, 15))

    def test_year_month_day(self):
        """YEAR, MONTH, DAY should extract components."""
        self.assertEqual(YEAR(self.d1), 2024)
        self.assertEqual(MONTH(self.d1), 1)
        self.assertEqual(DAY(self.d1), 15)

    def test_year_from_string(self):
        """YEAR should parse date strings."""
        self.assertEqual(YEAR("2024-06-15"), 2024)
        self.assertEqual(MONTH("2024-06-15"), 6)

    def test_days(self):
        """DAYS should return difference in days."""
        self.assertEqual(DAYS(self.d2, self.d1), 167)
        self.assertEqual(DAYS(self.d1, self.d2), -167)

    def test_edate(self):
        """EDATE should add months to a date."""
        result = EDATE(date(2024, 1, 31), 1)
        # Jan 31 + 1 month => Feb 29 (2024 is leap year)
        self.assertEqual(result, date(2024, 2, 29))
        result2 = EDATE(date(2024, 3, 15), -1)
        self.assertEqual(result2, date(2024, 2, 15))

    def test_eomonth(self):
        """EOMONTH should return last day of target month."""
        result = EOMONTH(date(2024, 1, 15), 0)
        self.assertEqual(result, date(2024, 1, 31))
        result2 = EOMONTH(date(2024, 1, 15), 1)
        self.assertEqual(result2, date(2024, 2, 29))  # Leap year

    def test_networkdays(self):
        """NETWORKDAYS should count business days."""
        # Mon Jan 1 to Fri Jan 5, 2024 => 5 workdays
        result = NETWORKDAYS(date(2024, 1, 1), date(2024, 1, 5))
        self.assertEqual(result, 5)

    def test_networkdays_with_holidays(self):
        """NETWORKDAYS should exclude holidays."""
        result = NETWORKDAYS(
            date(2024, 1, 1), date(2024, 1, 5),
            [date(2024, 1, 1)]  # New Year's Day
        )
        self.assertEqual(result, 4)

    def test_datedif(self):
        """DATEDIF should compute date differences in various units."""
        d1 = date(2023, 1, 1)
        d2 = date(2024, 6, 15)
        self.assertEqual(DATEDIF(d1, d2, "Y"), 1)
        self.assertEqual(DATEDIF(d1, d2, "M"), 17)


class ExcelInfoTests(unittest.TestCase):
    """Tests for Information functions."""

    def test_isblank(self):
        """ISBLANK should detect None and empty strings."""
        self.assertTrue(ISBLANK(None))
        self.assertTrue(ISBLANK(""))
        self.assertFalse(ISBLANK(0))
        self.assertFalse(ISBLANK("text"))

    def test_isnumber(self):
        """ISNUMBER should detect numeric types."""
        self.assertTrue(ISNUMBER(42))
        self.assertTrue(ISNUMBER(3.14))
        self.assertFalse(ISNUMBER("42"))
        self.assertFalse(ISNUMBER(True))

    def test_istext(self):
        """ISTEXT should detect string types."""
        self.assertTrue(ISTEXT("hello"))
        self.assertTrue(ISTEXT(""))
        self.assertFalse(ISTEXT(42))
        self.assertFalse(ISTEXT(None))

    def test_iserror(self):
        """ISERROR should detect NaN and Inf."""
        self.assertTrue(ISERROR(float("nan")))
        self.assertTrue(ISERROR(float("inf")))
        self.assertFalse(ISERROR(42))
        self.assertFalse(ISERROR("text"))

    def test_isna(self):
        """ISNA should detect None and NaN."""
        self.assertTrue(ISNA(None))
        self.assertTrue(ISNA(float("nan")))
        self.assertFalse(ISNA(0))

    def test_type_func(self):
        """TYPE should return correct type codes."""
        self.assertEqual(TYPE_FUNC(42), 1)       # Number
        self.assertEqual(TYPE_FUNC("text"), 2)    # Text
        self.assertEqual(TYPE_FUNC(True), 4)      # Logical
        self.assertEqual(TYPE_FUNC([1, 2]), 64)   # Array

    def test_n_func(self):
        """N should convert to numeric."""
        self.assertEqual(N(42), 42.0)
        self.assertEqual(N(True), 1.0)
        self.assertEqual(N(False), 0.0)
        self.assertEqual(N("hello"), 0.0)


class ExcelEngineeringTests(unittest.TestCase):
    """Tests for Engineering functions."""

    def test_bin2dec(self):
        """BIN2DEC should convert binary to decimal."""
        self.assertEqual(BIN2DEC("1010"), 10)
        self.assertEqual(BIN2DEC("11111111"), 255)
        self.assertEqual(BIN2DEC("0"), 0)

    def test_dec2bin(self):
        """DEC2BIN should convert decimal to binary."""
        self.assertEqual(DEC2BIN(10), "1010")
        self.assertEqual(DEC2BIN(255), "11111111")

    def test_hex2dec(self):
        """HEX2DEC should convert hex to decimal."""
        self.assertEqual(HEX2DEC("FF"), 255)
        self.assertEqual(HEX2DEC("A"), 10)
        self.assertEqual(HEX2DEC("1F"), 31)

    def test_dec2hex(self):
        """DEC2HEX should convert decimal to hex."""
        self.assertEqual(DEC2HEX(255), "FF")
        self.assertEqual(DEC2HEX(10), "A")

    def test_hex2bin(self):
        """HEX2BIN should convert hex to binary."""
        self.assertEqual(HEX2BIN("A"), "1010")

    def test_convert_length(self):
        """CONVERT should convert length units."""
        self.assertAlmostEqual(CONVERT(1, "m", "ft"), 3.28084, places=3)
        self.assertAlmostEqual(CONVERT(1, "km", "m"), 1000.0)

    def test_convert_temperature(self):
        """CONVERT should convert temperature units."""
        self.assertAlmostEqual(CONVERT(100, "C", "F"), 212.0)
        self.assertAlmostEqual(CONVERT(32, "F", "C"), 0.0)
        self.assertAlmostEqual(CONVERT(0, "C", "K"), 273.15)

    def test_convert_same_unit(self):
        """CONVERT with same unit should return input."""
        self.assertEqual(CONVERT(42, "m", "m"), 42.0)

    def test_convert_mass(self):
        """CONVERT should convert mass units."""
        self.assertAlmostEqual(CONVERT(1, "kg", "lbm"), 2.20462, places=3)


class ExcelDatabaseTests(unittest.TestCase):
    """Tests for Database functions."""

    def setUp(self):
        """Set up a sample database for testing."""
        self.db = [
            ["Name", "Department", "Salary"],
            ["Alice", "Engineering", 90000],
            ["Bob", "Engineering", 80000],
            ["Carol", "Sales", 70000],
            ["Dave", "Sales", 75000],
            ["Eve", "Engineering", 95000],
        ]

    def test_dsum(self):
        """DSUM should sum field for matching criteria."""
        criteria = [["Department"], ["Engineering"]]
        result = DSUM(self.db, "Salary", criteria)
        self.assertEqual(result, 265000.0)  # 90000 + 80000 + 95000

    def test_daverage(self):
        """DAVERAGE should average field for matching criteria."""
        criteria = [["Department"], ["Sales"]]
        result = DAVERAGE(self.db, "Salary", criteria)
        self.assertAlmostEqual(result, 72500.0)  # (70000 + 75000) / 2

    def test_dcount(self):
        """DCOUNT should count numeric values for matching criteria."""
        criteria = [["Department"], ["Engineering"]]
        result = DCOUNT(self.db, "Salary", criteria)
        self.assertEqual(result, 3)

    def test_dsum_numeric_criteria(self):
        """DSUM with numeric criteria should filter correctly."""
        criteria = [["Salary"], [">80000"]]
        result = DSUM(self.db, "Salary", criteria)
        self.assertEqual(result, 185000.0)  # 90000 + 95000


class ExcelEdgeCaseTests(unittest.TestCase):
    """Tests for edge cases: NaN handling, empty arrays, type coercion, etc."""

    def test_nan_handling_iferror(self):
        """IFERROR should catch NaN values."""
        self.assertEqual(IFERROR(float("nan"), 0), 0)
        self.assertEqual(IFERROR(float("inf"), -1), -1)
        self.assertEqual(IFERROR(42, -1), 42)

    def test_empty_array_sum(self):
        """SUM of empty input should return 0."""
        self.assertEqual(SUM_FUNC(), 0.0)
        self.assertEqual(SUM_FUNC([]), 0.0)

    def test_empty_array_count(self):
        """COUNT of empty input should return 0."""
        self.assertEqual(COUNT(), 0)

    def test_type_coercion_num(self):
        """_num helper should coerce various types to float."""
        self.assertEqual(_num(42), 42.0)
        self.assertEqual(_num("3.14"), 3.14)
        self.assertEqual(_num(True), 1.0)
        self.assertEqual(_num(False), 0.0)
        self.assertEqual(_num("not a number"), 0.0)
        self.assertEqual(_num(None), 0.0)

    def test_type_coercion_nums(self):
        """_nums should flatten and coerce nested lists."""
        self.assertEqual(_nums([1, [2, [3]]]), [1.0, 2.0, 3.0])
        self.assertEqual(_nums("hello", 5), [5.0])

    def test_criteria_matching_operators(self):
        """_match_criteria should handle comparison operators."""
        self.assertTrue(_match_criteria(10, ">5"))
        self.assertTrue(_match_criteria(10, ">=10"))
        self.assertFalse(_match_criteria(10, ">10"))
        self.assertTrue(_match_criteria(5, "<=5"))
        self.assertTrue(_match_criteria(5, "<>10"))
        self.assertTrue(_match_criteria(5, "=5"))

    def test_criteria_matching_wildcards(self):
        """_match_criteria should handle wildcard patterns."""
        self.assertTrue(_match_criteria("apple", "app*"))
        self.assertTrue(_match_criteria("apple", "a*e"))
        self.assertFalse(_match_criteria("banana", "app*"))
        self.assertTrue(_match_criteria("cat", "c?t"))
        self.assertFalse(_match_criteria("cart", "c?t"))

    def test_criteria_case_insensitive(self):
        """_match_criteria string comparison should be case insensitive."""
        self.assertTrue(_match_criteria("Apple", "apple"))
        self.assertTrue(_match_criteria("HELLO", "hello"))

    def test_sumif_with_criteria(self):
        """SUMIF should sum values where criteria match."""
        vals = [10, 20, 30, 40, 50]
        result = SUMIF(vals, ">25")
        self.assertEqual(result, 120.0)  # 30 + 40 + 50

    def test_countifs_multi_criteria(self):
        """COUNTIFS should support multiple criteria ranges."""
        ages = [25, 30, 35, 40, 45]
        scores = [80, 90, 85, 70, 95]
        result = COUNTIFS(ages, ">30", scores, ">=85")
        self.assertEqual(result, 2)  # (35, 85) and (45, 95)

    def test_sumproduct_empty(self):
        """SUMPRODUCT with empty arrays should return 0."""
        self.assertEqual(SUMPRODUCT([], []), 0.0)

    def test_string_number_coercion_in_sum(self):
        """SUM should coerce string numbers."""
        self.assertEqual(SUM_FUNC("1", "2", "3"), 6.0)
        # Non-numeric strings are silently skipped
        self.assertEqual(SUM_FUNC("1", "hello", "3"), 4.0)

    def test_nested_list_flattening(self):
        """SUM should handle deeply nested lists."""
        self.assertEqual(SUM_FUNC([[1, [2]], [3, [4, [5]]]]), 15.0)

    def test_excel_functions_registry_coverage(self):
        """EXCEL_FUNCTIONS dict should contain all major functions."""
        expected_keys = [
            "SUM", "AVERAGE", "IF", "VLOOKUP", "PMT", "CONCATENATE",
            "DATE", "ISBLANK", "BIN2DEC", "DSUM", "ROUND", "STDEV",
            "NPV", "AND", "LEFT", "INDEX", "YEAR", "ISNUMBER", "CONVERT",
        ]
        for key in expected_keys:
            self.assertIn(key, EXCEL_FUNCTIONS, f"{key} missing from EXCEL_FUNCTIONS")

    def test_excel_functions_callable(self):
        """All entries in EXCEL_FUNCTIONS should be callable."""
        for name, func in EXCEL_FUNCTIONS.items():
            self.assertTrue(callable(func), f"{name} is not callable")


class ExcelSumifCountifTests(unittest.TestCase):
    """Additional tests for conditional aggregation functions."""

    def test_sumif_with_separate_sum_range(self):
        """SUMIF should sum from a separate range."""
        categories = ["A", "B", "A", "B", "A"]
        amounts = [10, 20, 30, 40, 50]
        result = SUMIF(categories, "A", amounts)
        self.assertEqual(result, 90.0)  # 10 + 30 + 50

    def test_countif_wildcards(self):
        """COUNTIF should support wildcard patterns in criteria."""
        names = ["apple", "apricot", "banana", "avocado"]
        self.assertEqual(COUNTIF(names, "a*"), 3)  # apple, apricot, avocado


if __name__ == "__main__":
    unittest.main()
