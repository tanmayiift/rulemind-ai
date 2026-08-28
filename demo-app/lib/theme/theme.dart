import 'package:flutter/material.dart';
import 'tokens.dart';
/// Tabular figures.
///
/// The score, the currency values, the slider readouts and the condition
/// actuals all change as the user drags. Proportional digits make them jitter,
/// which reads as sloppy in a live demo. The pre-redesign code reached for this
/// and shipped `fontFeatures: const []` — an empty list, a no-op.
const List<FontFeature> kTabular = <FontFeature>[FontFeature.tabularFigures()];
/// The app's light theme.
///
/// Light only, and [ThemeMode.light] is set explicitly at the call site. The
/// app previously shipped a `darkTheme` with no `themeMode`, which meant it was
/// *reachable* from a phone's system setting — and since every colour in the
/// app was light-tuned and the dark theme did not even set a scaffold
/// background, anyone with a dark phone saw a broken app.
abstract final class RmTheme {
  static ThemeData get light {
    const ColorScheme scheme = ColorScheme.light(
      primary: RmC.accent,
      onPrimary: Colors.white,
      secondary: RmC.accent,
      onSecondary: Colors.white,
      surface: RmC.surface,
      onSurface: RmC.ink,
      error: Color(0xFFC62A2A),
      onError: Colors.white,
      outline: RmC.lineStrong,
      outlineVariant: RmC.line,
    );
    const TextTheme text = TextTheme(
      // The score. The number a decisioning buyer looks at first.
      displaySmall: TextStyle(
        fontSize: 34,
        fontWeight: FontWeight.w700,
        letterSpacing: -0.5,
        height: 1.1,
        fontFeatures: kTabular,
      ),
      // The verdict word.
      headlineSmall: TextStyle(
        fontSize: 24,
        fontWeight: FontWeight.w700,
        letterSpacing: -0.3,
        height: 1.15,
      ),
      titleLarge: TextStyle(
        fontSize: 22,
        fontWeight: FontWeight.w700,
        letterSpacing: -0.2,
      ),
      titleMedium: TextStyle(
        fontSize: 17,
        fontWeight: FontWeight.w600,
        letterSpacing: -0.1,
      ),
      titleSmall: TextStyle(fontSize: 15, fontWeight: FontWeight.w600),
      bodyLarge: TextStyle(fontSize: 16, height: 1.4),
      bodyMedium: TextStyle(fontSize: 15, height: 1.45),
      bodySmall: TextStyle(fontSize: 13, height: 1.45, color: RmC.inkMuted),
      labelLarge: TextStyle(
        fontSize: 15,
        fontWeight: FontWeight.w600,
        letterSpacing: 0.1,
      ),
      labelMedium: TextStyle(
        fontSize: 13,
        fontWeight: FontWeight.w600,
        letterSpacing: 0.2,
      ),
      // One job only: dense numeric micro-copy. It previously did four.
      labelSmall: TextStyle(
        fontSize: 11,
        fontWeight: FontWeight.w600,
        letterSpacing: 0.4,
        fontFeatures: kTabular,
      ),
    );
    return ThemeData(
      useMaterial3: true,
      colorScheme: scheme,
      scaffoldBackgroundColor: RmC.canvas,
      textTheme: text.apply(bodyColor: RmC.ink, displayColor: RmC.ink),
      // Insurance only: no app code reads hintColor any more, but framework
      // internals still do, and its default is far below AA.
      hintColor: RmC.inkMuted,
      dividerTheme: const DividerThemeData(
        color: RmC.line,
        thickness: 1,
        space: 1,
      ),
      appBarTheme: const AppBarTheme(
        backgroundColor: RmC.surface,
        foregroundColor: RmC.ink,
        surfaceTintColor: Colors.transparent,
        elevation: 0,
        scrolledUnderElevation: 0,
        centerTitle: false,
        titleTextStyle: TextStyle(
          fontSize: 18,
          fontWeight: FontWeight.w700,
          color: RmC.ink,
          letterSpacing: -0.2,
        ),
      ),
      // Depth comes from a hairline, not a shadow. One line, and it is the
      // single biggest legibility win available to this app.
      cardTheme: const CardThemeData(
        elevation: 0,
        color: RmC.surface,
        surfaceTintColor: Colors.transparent,
        margin: EdgeInsets.zero,
        shape: RoundedRectangleBorder(
          borderRadius: RmRadius.rLg,
          side: BorderSide(color: RmC.line),
        ),
      ),
      // Pinning cardTheme alone is not enough: chips, buttons and segmented
      // buttons each carry their own M3 stadium shape.
      // Dense on purpose. With default M3 chip padding these scenario labels
      // take a full row each, so four of them push the inputs below the fold --
      // and the scenario row is the first thing a prospect looks at.
      chipTheme: ChipThemeData(
        backgroundColor: RmC.surface,
        selectedColor: RmC.accentSoft,
        side: const BorderSide(color: RmC.line),
        shape: const RoundedRectangleBorder(borderRadius: RmRadius.rSm),
        showCheckmark: false,
        labelPadding: const EdgeInsets.symmetric(horizontal: RmSpace.x1),
        padding: const EdgeInsets.symmetric(
          horizontal: RmSpace.x2,
          vertical: RmSpace.x2,
        ),
        labelStyle: text.labelMedium?.copyWith(color: RmC.ink),
      ),
      filledButtonTheme: FilledButtonThemeData(
        style: FilledButton.styleFrom(
          backgroundColor: RmC.accent,
          foregroundColor: Colors.white,
          shape: const RoundedRectangleBorder(borderRadius: RmRadius.rMd),
          padding: const EdgeInsets.symmetric(vertical: RmSpace.x4),
          textStyle: text.labelLarge,
        ),
      ),
      textButtonTheme: TextButtonThemeData(
        style: TextButton.styleFrom(
          foregroundColor: RmC.accent,
          shape: const RoundedRectangleBorder(borderRadius: RmRadius.rSm),
          textStyle: text.labelMedium,
        ),
      ),
      segmentedButtonTheme: SegmentedButtonThemeData(
        style: SegmentedButton.styleFrom(
          backgroundColor: RmC.surface,
          foregroundColor: RmC.inkMuted,
          selectedBackgroundColor: RmC.accentSoft,
          selectedForegroundColor: RmC.accent,
          side: const BorderSide(color: RmC.line),
          shape: const RoundedRectangleBorder(borderRadius: RmRadius.rMd),
          textStyle: text.labelMedium,
        ),
      ),
      // Set once here so no widget re-points a slider or switch at a journey
      // colour again.
      sliderTheme: const SliderThemeData(
        activeTrackColor: RmC.accent,
        inactiveTrackColor: RmC.line,
        thumbColor: RmC.accent,
        trackHeight: 3,
        overlayColor: Color(0x1A3D3BA8),
      ),
      switchTheme: SwitchThemeData(
        thumbColor: WidgetStateProperty.resolveWith(
          (Set<WidgetState> s) =>
              s.contains(WidgetState.selected) ? Colors.white : RmC.inkFaint,
        ),
        trackColor: WidgetStateProperty.resolveWith(
          (Set<WidgetState> s) =>
              s.contains(WidgetState.selected) ? RmC.accent : RmC.surfaceSunk,
        ),
        trackOutlineColor: WidgetStateProperty.resolveWith(
          (Set<WidgetState> s) =>
              s.contains(WidgetState.selected) ? RmC.accent : RmC.lineStrong,
        ),
      ),
      progressIndicatorTheme: const ProgressIndicatorThemeData(
        color: RmC.accent,
        linearTrackColor: RmC.surfaceSunk,
      ),
      iconTheme: const IconThemeData(color: RmC.inkMuted),
    );
  }
}
