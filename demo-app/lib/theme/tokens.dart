import 'package:flutter/material.dart';

/// Design tokens for the RuleMind demo app.
///
/// Direction: **Slate & Signal** — a low-chroma neutral instrument surface,
/// hairline-ruled rather than shadowed, where saturated colour is a *reading*
/// and not decoration. The only chromatic pixels on a screen belong to a
/// verdict, a disclosure, or the single accent on the primary action.
///
/// The test for any colour decision: *if this screen were desaturated, would
/// information be lost?* If yes it is signal and keeps its hue. If no it
/// belongs to the neutral ramp.

// ── Neutral ramp ────────────────────────────────────────────────────────────

abstract final class RmC {
  /// Scaffold behind every surface.
  static const Color canvas = Color(0xFFF6F7F9);

  /// Cards, app bar, sheets.
  static const Color surface = Color(0xFFFFFFFF);

  /// Recessed wells: the input group, the empty state, the trace rail.
  static const Color surfaceSunk = Color(0xFFEFF1F5);

  /// The 1px hairline. This is the primary depth cue — the app uses borders,
  /// not shadows, which is what gives it a ruled instrument feel rather than a
  /// floaty consumer-card one.
  static const Color line = Color(0xFFE2E5EB);

  /// A divider that must actually be seen.
  static const Color lineStrong = Color(0xFFCED3DC);

  /// Primary text. 19.0:1 on [surface].
  static const Color ink = Color(0xFF11151C);

  /// Secondary text. 6.1:1 on [surface] — comfortably AA.
  static const Color inkMuted = Color(0xFF5A6372);

  /// **NON-TEXT ONLY.** 3.0:1 on [surface]: fails AA for text, clears the 3:1
  /// floor for icons and borders. Legal on chevrons, glyphs, dividers,
  /// disabled states, the empty-state mark. Illegal on any string.
  ///
  /// This is a hard rule, not a hint. The pre-redesign app used `hintColor`
  /// (≈2.6:1) for text *and* icons at 13 sites, which is exactly this mistake.
  static const Color inkFaint = Color(0xFF8A93A3);

  /// The one accent — the RuleMind indigo pulled down in lightness and chroma.
  ///
  /// At 8.8:1 on [surface] it works as text, as an icon, *and* as a full CTA
  /// fill with a white label. That is why the app needs one accent value
  /// rather than three.
  static const Color accent = Color(0xFF3D3BA8);

  /// Selected-chip fill.
  static const Color accentSoft = Color(0xFFEBEBF7);

  /// The mid-tone step the old palette never had — it jumped from an alpha
  /// wash to full saturation with nothing in between.
  static const Color accentLine = Color(0xFFC7C7E8);
}

// ── Semantic scale ──────────────────────────────────────────────────────────

/// A semantic colour in three tones, each with exactly one legal use.
///
/// Having three tones is the fix for the pre-redesign app's core failure: it
/// had a faint wash (alpha 0.12–0.18) and a shout (alpha 1.0) and nothing
/// between, so a banner or a badge had no calm-but-legible option.
@immutable
class RmTone {
  const RmTone({required this.base, required this.soft, required this.ink});

  /// The mark: icons ≥16dp, rails, dots, 2dp bars.
  /// Never a large fill. Never body text.
  final Color base;

  /// The container fill. Only ever a background.
  final Color soft;

  /// Text on [soft] and on [RmC.surface]. Never a fill.
  final Color ink;
}

abstract final class RmSem {
  /// ink-on-soft 7.65:1 (AAA)
  static const RmTone approve = RmTone(
    base: Color(0xFF12855A),
    soft: Color(0xFFE3F3EB),
    ink: Color(0xFF0A5537),
  );

  /// ink-on-soft 7.01:1 (AAA). Tight — do not lighten [RmTone.soft].
  static const RmTone review = RmTone(
    base: Color(0xFFA85F09),
    soft: Color(0xFFFBF0DE),
    ink: Color(0xFF7A4405),
  );

  /// ink-on-soft 7.60:1 (AAA)
  static const RmTone reject = RmTone(
    base: Color(0xFFC62A2A),
    soft: Color(0xFFFBE9E9),
    ink: Color(0xFF8F1D1D),
  );

  /// ink-on-soft 7.61:1 (AAA). Used only inside a bordered disclosure panel on
  /// the result surface, so it never shares a screen region with [RmC.accent].
  static const RmTone info = RmTone(
    base: Color(0xFF1367A6),
    soft: Color(0xFFE6F1F9),
    ink: Color(0xFF0F4E7D),
  );

  /// For an outcome the engine returned that this build does not recognise.
  /// Not an error tone — an unknown outcome is not a failure.
  static const RmTone neutral = RmTone(
    base: RmC.inkMuted,
    soft: RmC.surfaceSunk,
    ink: Color(0xFF3A4250),
  );

  static const List<RmTone> all = <RmTone>[approve, review, reject, info];

  /// The **only** way to derive a tone from an engine outcome.
  static RmTone forOutcome(String outcome) => switch (outcome) {
        'approve' => approve,
        'review' => review,
        'reject' => reject,
        _ => neutral,
      };
}

// ── Journey identity ────────────────────────────────────────────────────────

/// A journey's visual identity.
///
/// Deliberately **not** a `Color`. This type exists so that the brand/verdict
/// collision the redesign fixed cannot be reintroduced:
///
///   * the constructor is private, so a fifth identity cannot be created and an
///     arbitrary hex cannot be supplied;
///   * it carries only [mark] — there is no `soft` and no `ink` — so it is
///     incapable of filling a container or colouring a paragraph.
///
/// Before the redesign, the Telecom journey colour was **byte-identical** to
/// the `review` verdict colour, so on that screen the app bar, the primary
/// button, the sliders and the REVIEW badge were all the same amber and brand
/// was indistinguishable from verdict.
///
/// [mark] is legal in exactly two places: the 24dp glyph on a Home card (on a
/// neutral tile, not a tint of itself) and a 3dp rail under the journey app
/// bar. Never an app-bar fill, never a CTA, never on the result surface.
@immutable
class RmIdentity {
  const RmIdentity._(this.mark, this.debugName);

  final Color mark;
  final String debugName;

  /// Slate. Was orange-700, which sat inside the `reject` lane.
  static const RmIdentity claims = RmIdentity._(Color(0xFF3A5490), 'claims');

  /// Plum. Was violet-600, which sat on top of the accent indigo.
  static const RmIdentity checkout = RmIdentity._(Color(0xFF6E3D8C), 'checkout');

  /// Deep cyan-blue. Was a teal at 174°, only 16° from the `approve` base —
  /// close enough that a seller glyph could read as an approve verdict. Moved
  /// to 196°, which is 38° clear of the nearest verdict hue.
  static const RmIdentity seller = RmIdentity._(Color(0xFF2A5C6E), 'seller');

  /// Graphite. Was amber-700 — the one that collided with `review`, now the
  /// deliberately neutral member of the set.
  static const RmIdentity telecom = RmIdentity._(Color(0xFF4C5566), 'telecom');

  static const List<RmIdentity> values = <RmIdentity>[
    claims,
    checkout,
    seller,
    telecom,
  ];

  @override
  String toString() => 'RmIdentity($debugName)';
}

// ── Scale ───────────────────────────────────────────────────────────────────

/// A 4-pt spacing grid. The pre-redesign app used 13 distinct gaps, six of them
/// off any consistent step.
abstract final class RmSpace {
  static const double x1 = 4;
  static const double x2 = 8;
  static const double x3 = 12;
  static const double x4 = 16;
  static const double x5 = 20;
  static const double x6 = 24;
  static const double x8 = 32;
}

/// Three radii. Must be pinned in `cardTheme`, `chipTheme`, `filledButtonTheme`
/// and `segmentedButtonTheme` — pinning cards alone leaves Material 3's stadium
/// shapes in place, which is a fourth and fifth radius in practice.
abstract final class RmRadius {
  static const double sm = 8; // chips, badges, inline wells
  static const double md = 12; // icon tiles, buttons, inner containers
  static const double lg = 16; // cards, verdict bands

  static const BorderRadius rSm = BorderRadius.all(Radius.circular(sm));
  static const BorderRadius rMd = BorderRadius.all(Radius.circular(md));
  static const BorderRadius rLg = BorderRadius.all(Radius.circular(lg));
}
