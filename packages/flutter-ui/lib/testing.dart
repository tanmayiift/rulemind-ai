/// Responsive test harness shared by the RuleMind Flutter apps.
///
/// Not exported from `rulemind_ui.dart` and never imported by app code, so it
/// tree-shakes out of release builds.
library;

import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter_test/flutter_test.dart';

/// A device surface to exercise.
@immutable
class RmSurface {
  const RmSurface(this.name, this.size, {this.padding = EdgeInsets.zero});

  final String name;
  final Size size;

  /// Notch / home-indicator insets, so SafeArea work is actually exercised
  /// rather than run against zero insets.
  final EdgeInsets padding;

  @override
  String toString() => '$name (${size.width.toInt()}x${size.height.toInt()})';
}

/// The standard matrix. Every one of these is a real device configuration.
abstract final class RmSurfaces {
  static const EdgeInsets _notched =
      EdgeInsets.only(top: 47, bottom: 34);

  /// The compact worst case — 4" phones and any 1/3 split.
  static const RmSurface iphoneSe =
      RmSurface('iphone_se', Size(320, 568));

  /// The profile the CI emulator runs.
  static const RmSurface pixel5 =
      RmSurface('pixel_5', Size(393, 851), padding: _notched);

  static const RmSurface iphoneProMax =
      RmSurface('iphone_pro_max', Size(430, 932), padding: _notched);

  /// iPad Split View at 1/3 — narrow *and* tall, which breaks any layout that
  /// leans on height to infer "this is a tablet".
  static const RmSurface ipadSplit =
      RmSurface('ipad_split_1_3', Size(320, 1024), padding: _notched);

  static const RmSurface ipadPortrait =
      RmSurface('ipad_portrait', Size(834, 1112), padding: _notched);

  static const RmSurface ipadLandscape =
      RmSurface('ipad_landscape', Size(1194, 834), padding: _notched);

  static const List<RmSurface> all = <RmSurface>[
    iphoneSe,
    pixel5,
    iphoneProMax,
    ipadSplit,
    ipadPortrait,
    ipadLandscape,
  ];
}

/// Text scales to exercise. 2.0 is roughly iOS accessibility size AX3.
const List<double> kRmTextScales = <double>[1.0, 1.3, 2.0];

/// Applies [surface] to the test view and restores it afterwards.
void rmApplySurface(WidgetTester tester, RmSurface surface) {
  tester.view.devicePixelRatio = 1.0;
  tester.view.physicalSize = surface.size;
  addTearDown(tester.view.reset);
}

/// Wraps [child] in the MediaQuery a real device would supply.
Widget rmWrap(
  Widget child, {
  required RmSurface surface,
  required double textScale,
}) {
  return MediaQuery(
    data: MediaQueryData(
      size: surface.size,
      devicePixelRatio: 1.0,
      textScaler: TextScaler.linear(textScale),
      padding: surface.padding,
      viewPadding: surface.padding,
    ),
    child: child,
  );
}

/// Scrolls every scrollable to its end, pumping between steps.
///
/// This is the part people leave out, and without it the whole suite is
/// theatre: overflow is reported during **paint**, and a [ListView]'s children
/// below the fold are never painted. A screen that overflows three scrolls down
/// passes a naive `pumpWidget` at any size.
Future<void> rmScrollThrough(WidgetTester tester) async {
  final Iterable<Element> scrollables =
      find.byType(Scrollable).evaluate().toList();

  for (final Element element in scrollables) {
    final ScrollableState? state =
        element.widget is Scrollable ? (element as StatefulElement).state as ScrollableState? : null;
    if (state == null) continue;

    ScrollPosition position = state.position;
    if (position.axis != Axis.vertical) continue;

    int guard = 0;
    while (position.hasContentDimensions &&
        position.pixels < position.maxScrollExtent &&
        guard < 40) {
      final double target =
          (position.pixels + position.viewportDimension * 0.8)
              .clamp(0.0, position.maxScrollExtent);
      position.jumpTo(target);
      await tester.pump();
      guard++;
      position = state.position;
    }

    // Return to the top so later assertions see a stable frame.
    if (position.hasContentDimensions) {
      position.jumpTo(0);
      await tester.pump();
    }
  }
}


/// Names the widget behind a [RenderObject], plus its nearest named ancestors.
///
/// A bare `RenderStack spans -1.0..25.0` is unactionable; knowing it is the
/// thumb of a `Slider` inside a `_FieldRow` is the whole diagnosis.
String _describe(RenderObject node) {
  final String self = node.debugCreator is DebugCreator
      ? (node.debugCreator! as DebugCreator).element.widget.runtimeType.toString()
      : node.runtimeType.toString();

  final List<String> trail = <String>[];
  if (node.debugCreator is DebugCreator) {
    Element? e = (node.debugCreator! as DebugCreator).element;
    int hops = 0;
    e.visitAncestorElements((Element ancestor) {
      final String name = ancestor.widget.runtimeType.toString();
      if (!name.startsWith('_Render') && !name.startsWith('RawGesture')) {
        trail.add(name);
      }
      hops++;
      return trail.length < 4 && hops < 30;
    });
  }
  return trail.isEmpty ? self : '$self  (in ${trail.join(" < ")})';
}


/// True for render objects belonging to a Material chip's internal slots.
///
/// Material lays a chip's avatar/checkmark slot at a negative offset relative to
/// the chip and never paints it when hidden. Probed directly: a bare
/// `ChoiceChip` at 320x568 @1.3x reports its own box at 16.0..248.9 -- fully
/// inside the screen -- while an internal 26x26 Stack reports -1.0..25.0. The
/// chip is correctly positioned; only the hidden slot's coordinates are
/// negative, so flagging it is a false positive.
///
/// Scoped deliberately narrowly: this exempts chip internals, not chips, and
/// not anything else.
bool _isChipInternal(RenderObject node) {
  if (node.debugCreator is! DebugCreator) return false;
  bool found = false;
  int hops = 0;
  (node.debugCreator! as DebugCreator).element.visitAncestorElements((Element a) {
    if (a.widget.runtimeType.toString().contains('ChipRenderWidget')) {
      found = true;
      return false;
    }
    hops++;
    return hops < 8;
  });
  return found;
}

bool _insideHorizontalScrollable(RenderObject node) {
  RenderObject? current = node.parent;
  while (current != null) {
    if (current is RenderViewport && current.axis == Axis.horizontal) {
      return true;
    }
    if (current is RenderShrinkWrappingViewport &&
        current.axis == Axis.horizontal) {
      return true;
    }
    current = current.parent;
  }
  return false;
}

/// Asserts nothing paints past the left or right edge of the surface.
///
/// Flex overflow does not catch a `Text` that simply renders beyond the right
/// edge, because no `RenderFlex` is involved — so this is a separate check.
///
/// Subtrees inside a horizontally-scrollable viewport are exempt: a wide table
/// inside a horizontal `ListView` is correct behaviour, not a bug. The web
/// equivalent of this audit (`qa/e2e-full/frontend_responsive.mjs`) recorded 54
/// false positives on a single route before it made the same exemption.
void rmExpectNoHorizontalSpill(
  WidgetTester tester, {
  required RmSurface surface,
  required double textScale,
}) {
  final String where = '${surface.name} @ ${textScale}x';
  final List<String> offenders = <String>[];

  void visit(RenderObject node) {
    if (node is RenderBox && node.hasSize && node.attached) {
      final Size size = node.size;
      if (size.width > 0 &&
          size.height > 0 &&
          !_insideHorizontalScrollable(node) &&
          !_isChipInternal(node)) {
        try {
          final Offset origin = node.localToGlobal(Offset.zero);
          final double left = origin.dx;
          final double right = origin.dx + size.width;
          if (left < -0.5 || right > surface.size.width + 0.5) {
            offenders.add(
              '${_describe(node)} spans '
              '${left.toStringAsFixed(1)}..${right.toStringAsFixed(1)} '
              'outside 0..${surface.size.width.toStringAsFixed(0)}',
            );
          }
        } catch (_) {
          // Unattached or untransformable; nothing meaningful to assert.
        }
      }
    }
    node.visitChildren(visit);
  }

  final RenderObject? root = tester.binding.rootElement?.renderObject;
  if (root != null) visit(root);

  expect(
    offenders,
    isEmpty,
    reason: 'Horizontal spill at $where:\n  ${offenders.take(5).join('\n  ')}',
  );
}

/// Asserts interactive controls stay tappable (44x44 is the Apple HIG floor).
void rmExpectTapTargets(WidgetTester tester, {required String where}) {
  final List<String> tooSmall = <String>[];

  void check(Finder finder, String label) {
    for (final Element element in finder.evaluate()) {
      final RenderObject? ro = element.renderObject;
      if (ro is RenderBox && ro.hasSize) {
        final Size s = ro.size;
        if (s.width > 0 && s.height > 0 && (s.width < 44 || s.height < 44)) {
          tooSmall.add(
            '$label ${s.width.toStringAsFixed(0)}x${s.height.toStringAsFixed(0)}',
          );
        }
      }
    }
  }

  check(find.byType(IconButton), 'IconButton');

  expect(
    tooSmall,
    isEmpty,
    reason: 'Tap targets below 44x44 at $where:\n  ${tooSmall.take(5).join('\n  ')}',
  );
}

/// Runs [build] across the full surface x text-scale matrix.
///
/// Registers one `testWidgets` per combination so a failure names the exact
/// device configuration — relying on an implicit end-of-test failure would name
/// a widget but not the size that broke it, which nobody can reproduce.
void rmResponsiveMatrix(
  String description, {
  required Widget Function() build,
  List<RmSurface> surfaces = RmSurfaces.all,
  List<double> textScales = kRmTextScales,
  Future<void> Function(WidgetTester tester)? settle,
  bool checkTapTargets = true,
}) {
  for (final RmSurface surface in surfaces) {
    for (final double scale in textScales) {
      final String where = '${surface.name} @ ${scale}x';
      testWidgets('$description — $where', (WidgetTester tester) async {
        rmApplySurface(tester, surface);

        await tester.pumpWidget(
          rmWrap(build(), surface: surface, textScale: scale),
        );
        await tester.pump(const Duration(milliseconds: 100));

        if (settle != null) await settle(tester);

        await rmScrollThrough(tester);

        expect(
          tester.takeException(),
          isNull,
          reason: 'Layout exception (usually RenderFlex overflow) at $where',
        );

        rmExpectNoHorizontalSpill(
          tester,
          surface: surface,
          textScale: scale,
        );

        if (checkTapTargets && scale == 1.0) {
          rmExpectTapTargets(tester, where: where);
        }
      });
    }
  }
}
