// Proves the spill check still fails on a real overhang after being taught to
// respect clips. Without this, making the check clip-aware could silently
// reduce it to a no-op.
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:rulemind_ui/testing.dart';

const RmSurface _s = RmSurface('probe', Size(320, 568));

Future<void> _pump(WidgetTester tester, Widget child) async {
  rmApplySurface(tester, _s);
  await tester.pumpWidget(
    rmWrap(MaterialApp(home: child), surface: _s, textScale: 1.0),
  );
  await tester.pump(const Duration(milliseconds: 300));
}

void main() {
  group('detects a genuine unclipped overhang', () {
    testWidgets('unbreakable text wider than the screen',
        (WidgetTester tester) async {
      await _pump(tester, Scaffold(
        body: Stack(
          clipBehavior: Clip.none,
          children: <Widget>[
            Positioned(
              left: 10,
              child: Text('W' * 120, maxLines: 1, softWrap: false),
            ),
          ],
        ),
      ));
      expect(
        () => rmExpectNoHorizontalSpill(tester, surface: _s, textScale: 1.0),
        throwsA(isA<TestFailure>()),
      );
    });

    testWidgets('negatively-offset Positioned in an unclipped Stack',
        (WidgetTester tester) async {
      await _pump(tester, Scaffold(
        body: Stack(
          clipBehavior: Clip.none,
          children: <Widget>[
            Positioned(
              left: -60,
              top: 10,
              child: Container(width: 100, height: 30, color: Colors.blue),
            ),
          ],
        ),
      ));
      expect(
        () => rmExpectNoHorizontalSpill(tester, surface: _s, textScale: 1.0),
        throwsA(isA<TestFailure>()),
      );
    });
  });

  _screenClipRegression();

  group('ignores framework internals that are clipped by design', () {
    testWidgets('ChoiceChip hidden checkmark slot', (WidgetTester tester) async {
      await _pump(tester, Scaffold(
        body: Padding(
          padding: const EdgeInsets.all(16),
          child: Wrap(children: <Widget>[
            ChoiceChip(
              label: const Text('Clean claim'),
              selected: false,
              onSelected: (_) {},
            ),
          ]),
        ),
      ));
      rmExpectNoHorizontalSpill(tester, surface: _s, textScale: 1.0);
    });

    testWidgets('NavigationBar destination indicator',
        (WidgetTester tester) async {
      await _pump(tester, Scaffold(
        body: const SizedBox(),
        bottomNavigationBar: NavigationBar(
          selectedIndex: 0,
          destinations: const <Widget>[
            NavigationDestination(icon: Icon(Icons.home), label: 'Home'),
            NavigationDestination(icon: Icon(Icons.list), label: 'Scenarios'),
            NavigationDestination(icon: Icon(Icons.article), label: 'Logs'),
            NavigationDestination(icon: Icon(Icons.settings), label: 'Admin'),
          ],
        ),
      ));
      rmExpectNoHorizontalSpill(tester, surface: _s, textScale: 1.0);
    });

    testWidgets('TextField scrolling its own long content',
        (WidgetTester tester) async {
      await _pump(tester, Scaffold(
        body: Padding(
          padding: const EdgeInsets.all(16),
          child: TextFormField(
            initialValue:
                'a-fairly-long-value-that-exceeds-the-visible-field-width-by-a-lot',
            decoration: const InputDecoration(labelText: 'Value'),
          ),
        ),
      ));
      rmExpectNoHorizontalSpill(tester, surface: _s, textScale: 1.0);
    });
  });
}

// Regression: content clipped by the SCREEN is the bug this check exists to
// find, so the screen's own clip must not be treated as "safely clipped".
// Found on a real iOS simulator at 2.0x text, where a chip label was visibly
// truncated at the right edge while the matrix stayed green.
void _screenClipRegression() {
  testWidgets('text cut off by the screen edge is still reported',
      (WidgetTester tester) async {
    rmApplySurface(tester, _s);
    await tester.pumpWidget(rmWrap(
      MaterialApp(
        home: Scaffold(
          body: Align(
            alignment: Alignment.topLeft,
            child: UnconstrainedBox(
              alignment: Alignment.topLeft,
              constrainedAxis: Axis.vertical,
              child: SizedBox(
                width: 700,
                child: Text('W' * 90, maxLines: 1, softWrap: false),
              ),
            ),
          ),
        ),
      ),
      surface: _s,
      textScale: 1.0,
    ));
    await tester.pump();
    // UnconstrainedBox reports its own overflow during layout. Drain it so the
    // assertion below measures the spill check, not the framework's warning.
    tester.takeException();
    expect(
      () => rmExpectNoHorizontalSpill(tester, surface: _s, textScale: 1.0),
      throwsA(isA<TestFailure>()),
      reason: 'the screen clip must not hide an overhang',
    );
  });
}
