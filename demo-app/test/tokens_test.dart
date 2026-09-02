// Invariants on the design tokens.
//
// The redesign's central failure was structural, not cosmetic: the Telecom
// journey colour was BYTE-IDENTICAL to the `review` verdict colour, so on that
// screen brand and verdict were the same amber. These tests make that class of
// bug a red build rather than something a design review has to notice.
import 'dart:math' as math;

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:rulemind_journeys/theme/tokens.dart';

/// WCAG 2.1 relative luminance.
double _luminance(Color c) {
  double channel(double v) =>
      v <= 0.03928 ? v / 12.92 : math.pow((v + 0.055) / 1.055, 2.4).toDouble();
  return 0.2126 * channel(c.r) + 0.7152 * channel(c.g) + 0.0722 * channel(c.b);
}

/// WCAG 2.1 contrast ratio, 1.0–21.0.
double _contrast(Color a, Color b) {
  final double la = _luminance(a);
  final double lb = _luminance(b);
  return (math.max(la, lb) + 0.05) / (math.min(la, lb) + 0.05);
}

/// Hue in degrees, 0–360.
double _hue(Color c) => HSLColor.fromColor(c).hue;

/// HSL saturation, 0–1.
double _saturation(Color c) => HSLColor.fromColor(c).saturation;

/// Smallest angle between two hues, 0–180.
double _hueGap(Color a, Color b) {
  final double d = (_hue(a) - _hue(b)).abs() % 360;
  return d > 180 ? 360 - d : d;
}

void main() {
  final List<RmTone> tones = RmSem.all;

  group('brand identity can never be mistaken for a verdict', () {
    test('no journey identity equals any semantic tone', () {
      final List<Color> semantic = <Color>[
        for (final RmTone t in tones) ...<Color>[t.base, t.soft, t.ink],
      ];
      for (final RmIdentity id in RmIdentity.values) {
        expect(
          semantic,
          isNot(contains(id.mark)),
          reason: '$id is byte-identical to a verdict colour — this is exactly '
              'the Telecom/review clash the redesign removed',
        );
      }
    });

    test('no saturated identity sits within 25° of hue of a semantic base', () {
      for (final RmIdentity id in RmIdentity.values) {
        // A near-grey mark reads as neutral, not as a signal, so hue distance
        // stops being meaningful for it. Telecom is deliberately in this
        // bucket: it is the identity that used to BE the review colour, and it
        // is now the desaturated member of the set.
        if (_saturation(id.mark) < 0.25) continue;

        for (final RmTone t in tones) {
          // `info` is exempt: it appears only inside the disclosure panel on
          // the result surface, and identity marks appear only on Home and the
          // journey header rail, so the two never share a screen region.
          if (identical(t, RmSem.info)) continue;
          expect(
            _hueGap(id.mark, t.base),
            greaterThan(25),
            reason: '$id at ${_hue(id.mark).round()}° is too close to a verdict '
                'base at ${_hue(t.base).round()}°',
          );
        }
      }
    });

    test('the four journeys are distinguishable from each other', () {
      // Four look-alike glyphs would make the Home list unscannable, which is
      // the whole reason identity survives the redesign at all.
      final List<RmIdentity> saturated = RmIdentity.values
          .where((RmIdentity i) => _saturation(i.mark) >= 0.25)
          .toList();
      for (int i = 0; i < saturated.length; i++) {
        for (int j = i + 1; j < saturated.length; j++) {
          expect(
            _hueGap(saturated[i].mark, saturated[j].mark),
            greaterThan(20),
            reason: '${saturated[i]} and ${saturated[j]} are too close in hue',
          );
        }
      }
    });
  });

  group('contrast', () {
    test('every semantic ink clears 7:1 on its own soft', () {
      for (final RmTone t in tones) {
        expect(
          _contrast(t.ink, t.soft),
          greaterThanOrEqualTo(7.0),
          reason: 'ink ${t.ink} on soft ${t.soft} is not AAA — a filled '
              'container must stay legible without shouting',
        );
      }
    });

    test('every semantic ink clears 4.5:1 on the card surface', () {
      for (final RmTone t in tones) {
        expect(_contrast(t.ink, RmC.surface), greaterThanOrEqualTo(4.5));
      }
    });

    test('every semantic base clears the 3:1 icon floor on surface', () {
      for (final RmTone t in tones) {
        expect(_contrast(t.base, RmC.surface), greaterThanOrEqualTo(3.0));
      }
    });

    test('every journey mark clears 4.5:1 on surface', () {
      for (final RmIdentity id in RmIdentity.values) {
        expect(
          _contrast(id.mark, RmC.surface),
          greaterThanOrEqualTo(4.5),
          reason: '$id is used for the sector eyebrow, which is text',
        );
      }
    });

    test('text tokens meet AA; inkFaint is deliberately below it', () {
      expect(_contrast(RmC.ink, RmC.surface), greaterThanOrEqualTo(7.0));
      expect(_contrast(RmC.inkMuted, RmC.surface), greaterThanOrEqualTo(4.5));
      expect(_contrast(RmC.accent, RmC.surface), greaterThanOrEqualTo(4.5));
      expect(_contrast(Colors.white, RmC.accent), greaterThanOrEqualTo(4.5),
          reason: 'accent is used as a CTA fill with a white label');

      // Documents the rule rather than asserting a happy number: inkFaint is
      // NON-TEXT ONLY, and this test is what says so out loud.
      expect(
        _contrast(RmC.inkFaint, RmC.surface),
        lessThan(4.5),
        reason: 'inkFaint is for icons and borders; if it ever passes AA the '
            'comment claiming it is non-text has drifted',
      );
      expect(_contrast(RmC.inkFaint, RmC.surface), greaterThanOrEqualTo(3.0));
    });
  });

  group('the token set stays closed', () {
    test('there are exactly four journey identities', () {
      expect(RmIdentity.values, hasLength(4));
    });

    test('an unrecognised outcome is neutral, not an error', () {
      expect(RmSem.forOutcome('approve'), same(RmSem.approve));
      expect(RmSem.forOutcome('review'), same(RmSem.review));
      expect(RmSem.forOutcome('reject'), same(RmSem.reject));
      expect(RmSem.forOutcome('something_new'), same(RmSem.neutral));
    });
  });
}
