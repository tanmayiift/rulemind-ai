import 'package:flutter/material.dart';
import 'package:rulemind_journeys/engine.dart';
import 'package:rulemind_journeys/journeys.dart';
import 'package:rulemind_journeys/main.dart';
import 'package:rulemind_ui/testing.dart';

/// A result with every optional field populated and realistic-length strings.
///
/// The metadata row renders mode, latency, score and an expectation badge; a
/// result with nulls would skip most of it and quietly pass.
EvalResult _fullResult() => EvalResult(
      outcome: 'review',
      mode: EvalMode.onDevice,
      score: 742,
      latencyMs: 1284,
      gates: <GateResult>[
        GateResult(
          label: 'Claim history and prior settlement review',
          passed: false,
          conditions: <ConditionResult>[
            ConditionResult(
              variable: 'prior_claims_24m',
              operator: '<=',
              threshold: 2,
              value: 4,
              passed: false,
            ),
          ],
        ),
      ],
    );

void main() {
  final Journey journey = kJourneys.first;
  final RuleMindClient client = RuleMindClient(baseUrl: 'http://127.0.0.1:1');

  rmResponsiveMatrix(
    'HomeScreen',
    build: () => const RuleMindDemoApp(),
  );

  rmResponsiveMatrix(
    'JourneyScreen',
    build: () => MaterialApp(
      home: JourneyScreen(journey: journey, client: client),
    ),
  );

  rmResponsiveMatrix(
    'ResultCard',
    build: () => MaterialApp(
      home: Scaffold(
        body: ListView(
          children: <Widget>[
            ResultCard(
              result: _fullResult(),
              expected: 'approve',
              color: journey.color,
            ),
          ],
        ),
      ),
    ),
  );
}
