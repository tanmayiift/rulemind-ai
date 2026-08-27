// Visual-verification entry point for a real simulator or device.
//
// Widget tests render with a placeholder font, not the system font, so they
// cannot catch text that is clipped because of its real rendered width. That is
// not a hypothetical: at 2.0x text on an iPhone simulator the scenario chip
// "Serial claimant, new policy" was truncated by the screen edge while the
// 54-case responsive matrix stayed green.
//
// This boots straight into JourneyScreen -- which holds the densest layouts --
// so it can be inspected without needing tap injection (which requires either
// `sudo xcode-select` or accessibility permissions).
//
//   flutter build ios --simulator --debug \
//     -t lib/main_visual_probe.dart --dart-define=RM_TEXT_SCALE=2.0
//   xcrun simctl install <udid> build/ios/iphonesimulator/Runner.app
//   xcrun simctl launch  <udid> ai.rulemind.rulemindJourneys
//   xcrun simctl io      <udid> screenshot out.png
import 'package:flutter/material.dart';
import 'engine.dart';
import 'journeys.dart';
import 'main.dart';

void main() {
  const String scaleStr = String.fromEnvironment('RM_TEXT_SCALE', defaultValue: '1.0');
  final double scale = double.tryParse(scaleStr) ?? 1.0;

  runApp(MaterialApp(
    debugShowCheckedModeBanner: false,
    theme: ThemeData(
      colorScheme: ColorScheme.fromSeed(seedColor: const Color(0xFF4F46E5)),
      useMaterial3: true,
    ),
    builder: (BuildContext context, Widget? child) => MediaQuery(
      data: MediaQuery.of(context).copyWith(textScaler: TextScaler.linear(scale)),
      child: child!,
    ),
    home: JourneyScreen(journey: kJourneys.first, client: RuleMindClient()),
  ));
}
