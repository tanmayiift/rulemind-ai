import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter_test/flutter_test.dart';

Future<void> _probe(WidgetTester tester, String label, Widget child) async {
  tester.view.devicePixelRatio = 1.0;
  tester.view.physicalSize = const Size(320, 568);
  addTearDown(tester.view.reset);
  await tester.pumpWidget(MediaQuery(
    data: const MediaQueryData(size: Size(320, 568), devicePixelRatio: 1.0),
    child: MaterialApp(home: child),
  ));
  await tester.pump(const Duration(milliseconds: 300));
  debugPrint('--- $label ---');
  void visit(RenderObject n) {
    if (n is RenderBox && n.hasSize && n.size.width > 0) {
      try {
        final Offset o = n.localToGlobal(Offset.zero);
        final double r = o.dx + n.size.width;
        if (o.dx < -0.5 || r > 320.5) {
          debugPrint('  OUT ${n.runtimeType}: ${o.dx.toStringAsFixed(1)}..${r.toStringAsFixed(1)}');
        }
      } catch (_) {}
    }
    n.visitChildren(visit);
  }
  visit(tester.binding.rootElement!.renderObject!);
}

void main() {
  testWidgets('NavigationBar', (t) async {
    await _probe(t, 'NavigationBar', Scaffold(
      bottomNavigationBar: NavigationBar(
        selectedIndex: 0,
        destinations: const <Widget>[
          NavigationDestination(icon: Icon(Icons.home), label: 'Home'),
          NavigationDestination(icon: Icon(Icons.list), label: 'Scenarios'),
          NavigationDestination(icon: Icon(Icons.article), label: 'Logs'),
          NavigationDestination(icon: Icon(Icons.settings), label: 'Admin'),
        ],
      ),
      body: const SizedBox(),
    ));
    // Is the bar itself in bounds?
    final RenderBox bar = t.renderObject<RenderBox>(find.byType(NavigationBar));
    final Offset o = bar.localToGlobal(Offset.zero);
    debugPrint('  BAR OUTER: ${o.dx.toStringAsFixed(1)}..${(o.dx + bar.size.width).toStringAsFixed(1)}');
  });

  testWidgets('TextField', (t) async {
    await _probe(t, 'TextField', Scaffold(
      body: Padding(
        padding: const EdgeInsets.all(16),
        child: TextFormField(
          initialValue: 'a-fairly-long-value-that-exceeds-the-visible-field-width-by-a-lot',
          decoration: const InputDecoration(labelText: 'Value'),
        ),
      ),
    ));
    final RenderBox f = t.renderObject<RenderBox>(find.byType(TextFormField));
    final Offset o = f.localToGlobal(Offset.zero);
    debugPrint('  FIELD OUTER: ${o.dx.toStringAsFixed(1)}..${(o.dx + f.size.width).toStringAsFixed(1)}');
  });
}
