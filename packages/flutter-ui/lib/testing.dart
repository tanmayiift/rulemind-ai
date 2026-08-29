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






/// True for render objects that actually put pixels on the screen.
///
/// Layout-only boxes — `SizedBox`, `Padding`, `ConstrainedBox`, `Stack` — are
/// precisely what the framework parks outside its own bounds: a chip's hidden
/// checkmark slot, a `NavigationBar`'s destination indicator. They paint
/// nothing there, so their coordinates are not a defect. Restricting the check
/// to painters removes that entire class of false positive without weakening
/// it, because a real overhang is always something visible: text, an image, or
/// a filled shape.
bool _paintsContent(RenderObject node) {
  if (node is RenderParagraph ||
      node is RenderImage ||
      node is RenderDecoratedBox ||
      node is RenderPhysicalModel ||
      node is RenderPhysicalShape) {
    return true;
  }
  // ColoredBox's render object is private, so it can only be matched by name.
  return node.runtimeType.toString().contains('ColoredBox');
}

/// The region of [node] that can actually reach the screen, in global
/// coordinates, after every ancestor clip is applied.
///
/// Without this the check is unusable. Framework widgets routinely lay out
/// internals outside their own box and rely on a clip to hide them — probed
/// directly at 320x568:
///
///   * `ChoiceChip` sits at 16.0..248.9 while its hidden checkmark slot
///     reports -1.0..25.0;
///   * `NavigationBar` sits at 0.0..320.0 while its destination indicator
///     reports 280.0..344.0;
///   * `TextFormField` sits at 16.0..304.0 while its editable paint region
///     reports 16.0..1091.5, because a text field scrolls its content.
///
/// All three are correct rendering. Flagging them produced 44 failures that
/// said nothing about the app. Asking the framework what it clips — rather
/// than special-casing widget names — keeps the check honest and still catches
/// a genuinely unclipped overhang, such as an oversized SizedBox in a Column.
Rect? _visibleRect(RenderObject node) {
  if (node is! RenderBox || !node.hasSize) return null;

  Rect visible;
  try {
    visible = node.localToGlobal(Offset.zero) & node.size;
  } catch (_) {
    return null;
  }
  // A degenerate transform yields NaN, and every NaN comparison is false, so an
  // unguarded NaN would silently pass the bounds check rather than fail it.
  if (visible.left.isNaN ||
      visible.right.isNaN ||
      visible.top.isNaN ||
      visible.bottom.isNaN) {
    return null;
  }

  RenderObject child = node;
  RenderObject? parent = node.parent;
  int hops = 0;
  while (parent != null && hops < 200) {
    final Rect? clip = parent.describeApproximatePaintClip(child);
    if (clip != null) {
      try {
        final Offset origin = (parent as RenderBox).localToGlobal(Offset.zero);
        visible = visible.intersect(clip.shift(origin));
        if (visible.isEmpty) return Rect.zero;
      } catch (_) {
        // Parent is not a RenderBox (a sliver, say); its clip is not directly
        // comparable, so skip it rather than guess.
      }
    }
    child = parent;
    parent = parent.parent;
    hops++;
  }
  return visible;
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
      if (size.width > 0 && size.height > 0 && _paintsContent(node)) {
        final Rect? visible = _visibleRect(node);
        if (visible != null && !visible.isEmpty) {
          if (visible.left < -0.5 || visible.right > surface.size.width + 0.5) {
            offenders.add(
              '${_describe(node)} paints '
              '${visible.left.toStringAsFixed(1)}..${visible.right.toStringAsFixed(1)} '
              'outside 0..${surface.size.width.toStringAsFixed(0)}',
            );
          }
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
